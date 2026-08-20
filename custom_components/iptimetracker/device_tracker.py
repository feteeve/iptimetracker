from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME, DOMAIN
from .coordinator import IptimeDataUpdateCoordinator, WirelessClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    tracked: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        new_entities = []
        for client in coordinator.data.connected_clients:
            if client.mac not in tracked:
                tracked.add(client.mac)
                new_entities.append(IptimeDeviceTracker(coordinator, entry, client.mac))
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_add_new_devices)
    _add_new_devices()


class IptimeDeviceTracker(CoordinatorEntity[IptimeDataUpdateCoordinator], ScannerEntity):
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
        self._attr_unique_id = f"{DOMAIN}_{mac.replace(':', '_')}"
        self._last_seen: datetime | None = None

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
