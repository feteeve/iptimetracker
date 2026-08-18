from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IptimeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        IptimeWirelessCountSensor(coordinator, entry),
        IptimeDhcpCountSensor(coordinator, entry),
        IptimeStaticLeasesSensor(coordinator, entry),
    ])


class _IptimeBaseSensor(CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity):
    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{key}"


class IptimeWirelessCountSensor(_IptimeBaseSensor):
    _attr_name = "ipTIME 무선 접속 기기 수"
    _attr_native_unit_of_measurement = "대"
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "wireless_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.wireless_clients)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "devices": [
                {
                    "mac": c.mac,
                    "hostname": c.hostname,
                    "ip": c.ip,
                    "interface": c.interface,
                    "rssi_dbm": c.rssi,
                }
                for c in self.coordinator.data.wireless_clients
            ]
        }


class IptimeDhcpCountSensor(_IptimeBaseSensor):
    _attr_name = "ipTIME DHCP 임대 수"
    _attr_native_unit_of_measurement = "대"
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "dhcp_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.dhcp_leases)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "leases": [
                {
                    "mac": lease.mac,
                    "ip": lease.ip,
                    "hostname": lease.hostname,
                    "expires": lease.expires,
                }
                for lease in self.coordinator.data.dhcp_leases
            ]
        }


class IptimeStaticLeasesSensor(_IptimeBaseSensor):
    _attr_name = "ipTIME 고정IP 설정 수"
    _attr_native_unit_of_measurement = "개"
    _attr_icon = "mdi:ip"

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "static_count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.static_leases)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "static_leases": [
                {
                    "mac": s.mac,
                    "ip": s.ip,
                    "hostname": s.hostname,
                }
                for s in self.coordinator.data.static_leases
            ]
        }
