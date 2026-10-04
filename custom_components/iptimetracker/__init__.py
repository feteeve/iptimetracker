from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import IptimeClient, IptimeDataUpdateCoordinator
from .dhcp import DhcpReservations

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

ADD_DHCP_RESERVATION = "add_dhcp_reservation"
ADD_DHCP_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("mac"): str,
        vol.Required("ip"): str,
        vol.Optional("description", default=""): str,
    }
)


async def _async_add_dhcp_reservation(hass: HomeAssistant, call: ServiceCall) -> None:
    coordinator = hass.data.get(DOMAIN, {}).get(call.data["config_entry_id"])
    if coordinator is None:
        raise HomeAssistantError("ipTIME 구성 항목을 찾을 수 없습니다")
    try:
        updated = await coordinator.dhcp_reservations.add(
            call.data["mac"], call.data["ip"], call.data["description"]
        )
    except (ValueError, TypeError) as err:
        raise HomeAssistantError(str(err)) from err
    # Update the cached snapshot so the sensor reflects the verified write.
    diagnostics = dict(coordinator.data.diagnostics)
    raw = dict(diagnostics.get("raw") or {})
    raw["dhcp.reservations"] = [
        {"mac": row["mac"], "ip": row["ip"], "desc": row["name"]}
        for row in updated
    ]
    diagnostics["raw"] = raw
    coordinator.data.diagnostics = diagnostics
    coordinator._diagnostics = diagnostics
    coordinator.async_set_updated_data(coordinator.data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = IptimeClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = IptimeDataUpdateCoordinator(hass, client, entry)
    coordinator.dhcp_reservations = DhcpReservations(client)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _migrate_entity_unique_ids(hass, entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await client.close()
        raise

    if not hass.services.has_service(DOMAIN, ADD_DHCP_RESERVATION):
        async def handle_add(call: ServiceCall) -> None:
            await _async_add_dhcp_reservation(hass, call)

        hass.services.async_register(
            DOMAIN, ADD_DHCP_RESERVATION, handle_add, schema=ADD_DHCP_SCHEMA
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


def _migrate_entity_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Replace entry-id-scoped entity IDs with stable router-scoped IDs."""
    old_prefix = f"{DOMAIN}_{entry.entry_id}_"
    stable_scope = entry.unique_id or entry.data[CONF_HOST]
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.platform != DOMAIN:
            continue
        if not registry_entry.unique_id.startswith(old_prefix):
            continue
        registry.async_update_entity(
            registry_entry.entity_id,
            new_unique_id=f"{stable_scope}_{registry_entry.unique_id[len(old_prefix):]}",
        )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Normalize router addresses and unique IDs from pre-1.3 releases."""
    if entry.version >= 2:
        return True
    try:
        normalized_host = IptimeClient.normalize_host(entry.data[CONF_HOST])
    except (KeyError, TypeError, ValueError):
        _LOGGER.error("Cannot migrate ipTIME entry %s: invalid host", entry.entry_id)
        return False
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_HOST: normalized_host},
        unique_id=normalized_host,
        version=2,
    )
    _LOGGER.info("Migrated ipTIME entry %s to normalized host", entry.entry_id)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options (consider_home, RSSI limit) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.close()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, ADD_DHCP_RESERVATION)

    return unload_ok
