import json
import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError, LoginError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from .const import DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN, MANUFACTURER, PLATFORMS

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Area API helpers (using the confirmed /v3/devices/group/ endpoints)
# ---------------------------------------------------------------------------

async def _get_areas(api: HikConnect, device_serial: str) -> list[dict]:
    """
    Fetch NVR area (group) list from the confirmed Hik-Connect cloud endpoint.

    GET /v3/devices/group/{serial}/list

    Returns a list of raw area dicts.  Each dict is expected to contain at
    minimum: groupId (int), groupName (str), mode (int: 0=disarm, 1=arm, 2=arm-silent).
    """
    url = f"{api.BASE_URL}/v3/devices/group/{device_serial}/list"
    _LOGGER.debug("Fetching areas for device '%s': %s", device_serial, url)
    try:
        async with api.client.get(url, raise_for_status=False) as resp:
            if resp.status == 200:
                data = await resp.json()
                # Try common response wrapper keys
                for key in ("groupList", "list", "data", "groups"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # No recognized wrapper – return empty list
                return []
            _LOGGER.debug(
                "Area list returned HTTP %s for device '%s'", resp.status, device_serial
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Area list failed for device '%s': %s", device_serial, exc)
    return []


async def _get_area_detail(api: HikConnect, device_serial: str, group_id: int) -> dict:
    """
    Fetch the detail of a single NVR area, including its member cameras (resources).

    GET /v3/devices/group/{serial}/{groupId}

    Returns the raw response dict, typically containing 'resourceIds' and/or
    a list of resource objects describing the cameras in the area.
    On any failure returns an empty dict.
    """
    url = f"{api.BASE_URL}/v3/devices/group/{device_serial}/{group_id}"
    _LOGGER.debug(
        "Fetching area detail for device '%s' groupId=%s", device_serial, group_id
    )
    try:
        async with api.client.get(url, raise_for_status=False) as resp:
            if resp.status == 200:
                return await resp.json()
            _LOGGER.debug(
                "Area detail returned HTTP %s for device='%s' groupId=%s",
                resp.status, device_serial, group_id,
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug(
            "Area detail failed for device='%s' groupId=%s: %s",
            device_serial, group_id, exc,
        )
    return {}



async def _switch_defence_mode(
    api: HikConnect, device_serial: str, group_id: int, mode: int
) -> None:
    """
    Arm or disarm an NVR area group.

    POST /v3/devices/group/{serial}/switchDefenceMode
    Body: {"groupId": <int>, "mode": <0|1|2>}
      mode=0 → disarm
      mode=1 → arm (away)
      mode=2 → arm silent (home/stay)
    """
    url = f"{api.BASE_URL}/v3/devices/group/{device_serial}/switchDefenceMode"
    _LOGGER.debug(
        "switchDefenceMode device='%s' groupId=%s mode=%s", device_serial, group_id, mode
    )
    async with api.client.post(
        url, json={"groupId": group_id, "mode": mode}, raise_for_status=True
    ) as resp:
        await resp.read()


async def _create_area(
    api: HikConnect, device_serial: str, group_name: str, resource_ids: list[str]
) -> dict:
    """
    Create a new NVR area (group).

    POST /v3/devices/group/{serial}
    Body: {"groupName": <str>, "resourceIds": [<str>, ...]}

    Returns the raw response dict.
    Raises RuntimeError if the API reports failure in the meta.code field.
    """
    url = f"{api.BASE_URL}/v3/devices/group/{device_serial}"
    _LOGGER.warning(
        "[hikconnect] Creating area '%s' on device '%s' with resources %s",
        group_name, device_serial, resource_ids,
    )
    async with api.client.post(
        url,
        json={"groupName": group_name, "resourceIds": resource_ids},
        raise_for_status=True,
    ) as resp:
        data = await resp.json()

    meta_code = str(data.get("meta", {}).get("code", "?"))
    meta_msg = data.get("meta", {}).get("message", "")
    _LOGGER.warning(
        "[hikconnect] create_area response: code=%s message=%s full=%s",
        meta_code, meta_msg, data,
    )
    if meta_code not in ("200", "0"):
        raise RuntimeError(
            f"create_area API error: code={meta_code!r} message={meta_msg!r}"
        )
    return data


async def _delete_area(api: HikConnect, group_id: int) -> None:
    """
    Delete an NVR area (group).

    POST /v3/open/trust/v1/group/destroy?groupId=<id>

    Raises RuntimeError if the API reports failure in the meta.code field.
    """
    url = f"{api.BASE_URL}/v3/open/trust/v1/group/destroy"
    _LOGGER.warning("[hikconnect] Deleting area groupId=%s", group_id)
    async with api.client.post(
        url, params={"groupId": str(group_id)}, raise_for_status=True
    ) as resp:
        data = await resp.json()

    meta_code = str(data.get("meta", {}).get("code", "?"))
    meta_msg = data.get("meta", {}).get("message", "")
    _LOGGER.warning(
        "[hikconnect] delete_area response: code=%s message=%s full=%s",
        meta_code, meta_msg, data,
    )
    if meta_code not in ("200", "0"):
        raise RuntimeError(
            f"delete_area API error: code={meta_code!r} message={meta_msg!r}"
        )



# ---------------------------------------------------------------------------
# Integration setup
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    api = HikConnect()
    api.BASE_URL = entry.data["base_url"]

    try:
        await api.login(entry.data["username"], entry.data["password"])
    except LoginError as e:
        # TODO add config_flow reauthenticate handler
        raise ConfigEntryAuthFailed from e
    except aiohttp.ClientError as e:
        raise ConfigEntryNotReady from e

    async def relogin_if_needed():
        needed = api.is_refresh_login_needed()
        _LOGGER.debug("Relogin %s needed", ("IS" if needed else "IS NOT"))
        if needed:
            try:
                await api.refresh_login()
            except LoginError as e:
                # TODO add config_flow reauthenticate handler
                raise ConfigEntryAuthFailed from e

    async def async_update():
        try:
            await relogin_if_needed()
            _LOGGER.info("Getting devices")
            # Skip devices with malformed payload - see #62.
            devices = []
            it = api.get_devices()
            while True:
                try:
                    device = await it.__anext__()
                except StopAsyncIteration:
                    break
                except json.JSONDecodeError as e:
                    _LOGGER.warning("Skipping device with malformed data: %s", e)
                    continue
                devices.append(device)
            for device_info in devices:
                _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
                cameras = [c async for c in api.get_cameras(device_info["serial"])]
                device_info.update({"cameras": cameras})

                _LOGGER.info("Getting areas for device: '%s'", device_info["serial"])
                areas = await _get_areas(api, device_info["serial"])
                # Enrich each area with its member camera list
                for area in areas:
                    group_id = area.get("groupId")
                    if group_id is not None:
                        detail = await _get_area_detail(api, device_info["serial"], group_id)
                        # Normalise the resources list from whichever key the API uses.
                        # Confirmed response shape:
                        #   {"list": [{"groupId":…, "groupDevSerial":…, "memberId":"<cameraId>"}, …]}
                        resources = (
                            detail.get("list")
                            or detail.get("resourceList")
                            or detail.get("resources")
                            or detail.get("resourceIds")
                            or detail.get("cameraList")
                            or []
                        )
                        area["resources"] = resources
                device_info["areas"] = areas
                if areas:
                    _LOGGER.info(
                        "Found %d area(s) for device '%s'", len(areas), device_info["serial"]
                    )
                else:
                    _LOGGER.debug(
                        "No areas found for device '%s' (device may not support area management)",
                        device_info["serial"],
                    )

            return devices
        except (HikConnectError, aiohttp.ClientError) as e:
            raise UpdateFailed(e) from e

    # Refreshing device info can be relativelly infrequent, but...
    # BEWARE: Multiple people reported that they needed to restart the
    # integration every 24h / 48h. This is suspiciously regular.
    # There is probably a race condition between `update_interval`
    # and `api.is_refresh_login_needed()` => let's update it more often
    # than once per hour.
    # see: https://github.com/tomasbedrich/home-assistant-hikconnect/issues/27
    scan_interval_minutes = entry.options.get(
        "scan_interval_minutes", DEFAULT_SCAN_INTERVAL_MINUTES
    )
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update,
        update_interval=timedelta(minutes=scan_interval_minutes),
    )
    await coordinator.async_config_entry_first_refresh()

    dr = device_registry.async_get(hass)
    expected_identifiers: set[tuple[str, str]] = set()
    for device in coordinator.data:
        ha_device_id = (DOMAIN, device["id"])
        expected_identifiers.add(ha_device_id)
        dr.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={ha_device_id},
            name=device["name"],
            manufacturer=MANUFACTURER,
            model=device["type"],
            sw_version=device["version"],
        )
        for camera in device["cameras"]:
            # see: https://github.com/tomasbedrich/home-assistant-hikconnect/issues/60
            if not camera["is_shown"]:
                continue
            if not device["locks"].get(camera["channel_number"], 0):
                continue
            ha_camera_id = (DOMAIN, device["id"] + "-" + camera["id"])
            expected_identifiers.add(ha_camera_id)
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={ha_camera_id},
                name=camera["name"],
                manufacturer=MANUFACTURER,
                via_device=ha_device_id,
            )

    # Drop orphan devices created by previous versions.
    for ha_device in device_registry.async_entries_for_config_entry(
        dr, entry.entry_id
    ):
        if not any(ident in expected_identifiers for ident in ha_device.identifiers):
            dr.async_update_device(
                ha_device.id, remove_config_entry_id=entry.entry_id
            )

    # TODO handle multiple instances of the same integration
    hass.data[DOMAIN] = {
        "api": api,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # -----------------------------------------------------------------------
    # Register area management services
    # -----------------------------------------------------------------------

    async def handle_create_area(call: ServiceCall) -> None:
        """Service: hikconnect.create_area — create a new NVR area/group."""
        device_serial = call.data["device_serial"]
        group_name = call.data["group_name"]
        resource_ids = call.data["resource_ids"]
        try:
            result = await _create_area(api, device_serial, group_name, resource_ids)
            _LOGGER.warning(
                "[hikconnect] create_area succeeded: '%s' on device '%s' → %s",
                group_name, device_serial, result,
            )
            await coordinator.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("[hikconnect] create_area FAILED: %s", exc)

    async def handle_delete_area(call: ServiceCall) -> None:
        """Service: hikconnect.delete_area — delete an existing NVR area/group."""
        group_id = call.data["group_id"]
        try:
            await _delete_area(api, group_id)
            _LOGGER.warning("[hikconnect] delete_area succeeded: groupId=%s", group_id)
            await coordinator.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("[hikconnect] delete_area FAILED: %s", exc)

    async def handle_update_area(call: ServiceCall) -> None:
        """
        Service: hikconnect.update_area — replace the cameras in an existing area.

        Because the Hik-Connect API does not expose a PATCH endpoint for areas,
        this service deletes the existing area and immediately recreates it with
        the same name but new resource_ids.

        NOTE: Deletion + recreation changes the groupId.  The HA entity unique_id
        will stay the same session (coordinator refresh picks up the new groupId).
        """
        device_serial = call.data["device_serial"]
        group_id = call.data["group_id"]
        group_name = call.data["group_name"]
        resource_ids = call.data["resource_ids"]
        try:
            _LOGGER.warning(
                "[hikconnect] update_area: deleting groupId=%s on device '%s'",
                group_id, device_serial,
            )
            await _delete_area(api, group_id)
            result = await _create_area(api, device_serial, group_name, resource_ids)
            _LOGGER.warning(
                "[hikconnect] update_area: recreated area '%s' → %s", group_name, result
            )
            await coordinator.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("[hikconnect] update_area FAILED: %s", exc)

    hass.services.async_register(
        DOMAIN,
        "create_area",
        handle_create_area,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_name"): cv.string,
                vol.Required("resource_ids"): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "delete_area",
        handle_delete_area,
        schema=vol.Schema(
            {
                vol.Required("group_id"): vol.Coerce(int),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "update_area",
        handle_update_area,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_id"): vol.Coerce(int),
                vol.Required("group_name"): cv.string,
                vol.Required("resource_ids"): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
    )

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version == 1:
        new = {**entry.data, "base_url": HikConnect.BASE_URL}
        entry.version = 2
        hass.config_entries.async_update_entry(entry, data=new)

    _LOGGER.info("Migration to version %s successful", entry.version)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.services.async_remove(DOMAIN, "create_area")
        hass.services.async_remove(DOMAIN, "delete_area")
        hass.services.async_remove(DOMAIN, "update_area")
        data = hass.data.pop(DOMAIN)
        await data["api"].close()
    return unload_ok
