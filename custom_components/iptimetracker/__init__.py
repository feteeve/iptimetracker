from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import IptimeClient, IptimeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = IptimeClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = IptimeDataUpdateCoordinator(hass, client, entry)

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

    return unload_ok
