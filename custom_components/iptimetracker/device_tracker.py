from __future__ import annotations

import logging
import re
from datetime import datetime

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME, DOMAIN
from .coordinator import IptimeDataUpdateCoordinator, WirelessClient

_LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1
_MAC_UNIQUE_ID_PATTERN = re.compile(
    r"(?:^|_)([0-9A-Fa-f]{2}(?:[:_-][0-9A-Fa-f]{2}){5})$"
)


def _unique_id(entry: ConfigEntry, mac: str) -> str:
    return f"{DOMAIN}_{entry.entry_id}_{mac.replace(':', '_')}"


def _mac_from_unique_id(unique_id: str) -> str | None:
    match = _MAC_UNIQUE_ID_PATTERN.search(unique_id)
    if not match:
        return None
    return match.group(1).upper().replace("_", ":").replace("-", ":")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    store: Store[dict[str, list[str]]] = Store(
        hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.known_devices"
    )
    stored = await store.async_load() or {}
    tracked: set[str] = set()
    for stored_mac in stored.get("macs", []):
        if mac := _mac_from_unique_id(stored_mac):
            tracked.add(mac)

    # Recover devices registered by older releases even if they are away at
    # startup, and migrate their MAC-only unique IDs to router-scoped IDs.
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain != "device_tracker" or registry_entry.platform != DOMAIN:
            continue
        mac = _mac_from_unique_id(registry_entry.unique_id)
        if not mac:
            continue
        tracked.add(mac)
        expected_unique_id = _unique_id(entry, mac)
        if registry_entry.unique_id != expected_unique_id:
            registry.async_update_entity(
                registry_entry.entity_id, new_unique_id=expected_unique_id
            )

    added: set[str] = set()

    async def _save_known_devices() -> None:
        await store.async_save({"macs": sorted(tracked)})

    @callback
    def _add_new_devices() -> None:
        changed = False
        for client in coordinator.data.connected_clients:
            if client.mac not in tracked:
                tracked.add(client.mac)
                changed = True
        if changed:
            hass.async_create_task(_save_known_devices())

        new_entities = []
        for mac in sorted(tracked):
            if mac not in added:
                added.add(mac)
                new_entities.append(IptimeDeviceTracker(coordinator, entry, mac))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))
    _add_new_devices()


class IptimeDeviceTracker(
    CoordinatorEntity[IptimeDataUpdateCoordinator], ScannerEntity, RestoreEntity
):
    """Track a device currently connected to the ipTIME router.

    A device that briefly drops off the client list (Wi-Fi power save, a
    weak-signal hiccup) is kept "home" for CONF_CONSIDER_HOME seconds instead
    of flipping to "away" immediately, to avoid presence flapping.
    """

    _attr_source_type = SourceType.ROUTER

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry, mac: str
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._entry = entry
        self._attr_unique_id = _unique_id(entry, mac)
        self._last_seen: datetime | None = None

    @property
    def unique_id(self) -> str:
        """Scope a client to this router instead of using the MAC alone."""
        return self._attr_unique_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous_state = await self.async_get_last_state()
        if previous_state is None:
            return
        last_seen = previous_state.attributes.get("last_seen")
        if isinstance(last_seen, str):
            self._last_seen = dt_util.parse_datetime(last_seen)

    @property
    def _client(self) -> WirelessClient | None:
        for c in self.coordinator.data.connected_clients:
            if c.mac == self._mac:
                return c
        return None

    @property
    def is_connected(self) -> bool:
        if self._client is not None:
            self._last_seen = dt_util.utcnow()
            return True

        if self._last_seen is None:
            return False

        consider_home = self._entry.options.get(
            CONF_CONSIDER_HOME,
            self._entry.data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
        )
        return (dt_util.utcnow() - self._last_seen).total_seconds() < consider_home

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def hostname(self) -> str | None:
        c = self._client
        # DHCP 목록에서 더 정확한 hostname 보완
        if c and c.hostname and c.hostname != self._mac:
            return c.hostname
        for lease in self.coordinator.data.dhcp_leases:
            if lease.mac == self._mac and lease.hostname and lease.hostname != self._mac:
                return lease.hostname
        return self._mac

    @property
    def name(self) -> str:
        return self.hostname or self._mac

    @property
    def ip_address(self) -> str | None:
        c = self._client
        if c and c.ip:
            return c.ip
        for lease in self.coordinator.data.dhcp_leases:
            if lease.mac == self._mac:
                return lease.ip
        return None

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"mac": self._mac}
        c = self._client
        if c:
            attrs["interface"] = c.interface
            if c.rssi is not None:
                attrs["rssi_dbm"] = c.rssi
        if self._last_seen is not None:
            attrs["last_seen"] = self._last_seen.isoformat()

        # 고정IP 여부
        for static in self.coordinator.data.static_leases:
            if static.mac == self._mac:
                attrs["static_ip"] = static.ip
                break

        # DHCP 만료 시간
        for lease in self.coordinator.data.dhcp_leases:
            if lease.mac == self._mac and lease.expires:
                attrs["dhcp_expires"] = lease.expires
                break

        return attrs
