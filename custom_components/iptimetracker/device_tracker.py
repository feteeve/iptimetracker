from __future__ import annotations

import logging

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
        for client in coordinator.data.wireless_clients:
            if client.mac not in tracked:
                tracked.add(client.mac)
                new_entities.append(IptimeDeviceTracker(coordinator, client.mac))
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_add_new_devices)
    _add_new_devices()


class IptimeDeviceTracker(CoordinatorEntity[IptimeDataUpdateCoordinator], ScannerEntity):
    """ipTIME 무선 접속 기기 트래커."""

    _attr_source_type = SourceType.ROUTER

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{DOMAIN}_{mac.replace(':', '_')}"

    @property
    def _client(self) -> WirelessClient | None:
        for c in self.coordinator.data.wireless_clients:
            if c.mac == self._mac:
                return c
        return None

    @property
    def is_connected(self) -> bool:
        return self._client is not None

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
