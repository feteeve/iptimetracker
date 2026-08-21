from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfDataRate
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
    async_add_entities([IptimeWanLinkSpeedSensor(coordinator, entry)])


class IptimeWanLinkSpeedSensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity
):
    """Negotiated link speed of the router's WAN (internet) port.

    Pairs with the WAN connectivity binary_sensor: a working link that's
    unexpectedly slow (e.g. 100Mbps instead of the usual 1000Mbps) is a
    useful hint that the ISP side is degraded rather than fully down.
    """

    _attr_name = "ipTIME WAN 링크 속도"
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_icon = "mdi:speedometer"

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_wan_link_speed"

    @property
    def available(self) -> bool:
        wan = self.coordinator.data.wan_link
        return super().available and wan is not None and wan.connected

    @property
    def native_value(self) -> int | None:
        wan = self.coordinator.data.wan_link
        return wan.speed_mbps if wan else None
