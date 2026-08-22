from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfDataRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, entity_unique_id
from .coordinator import IptimeDataUpdateCoordinator
from .entity import GracefulAvailabilityMixin

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            IptimeWanLinkSpeedSensor(coordinator, entry),
            IptimeMeshStationCountSensor(coordinator, entry),
        ]
    )


class IptimeWanLinkSpeedSensor(
    GracefulAvailabilityMixin,
    CoordinatorEntity[IptimeDataUpdateCoordinator],
    SensorEntity,
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
        self._attr_unique_id = entity_unique_id(entry, "wan_link_speed")

    @property
    def _is_healthy(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.wan_link is not None
        )

    @property
    def available(self) -> bool:
        wan = self.coordinator.data.wan_link
        if wan is not None and not wan.connected:
            # A confirmed "no link" report, not a comms hiccup - no speed to
            # show, so this one goes unavailable immediately rather than
            # waiting out the grace period below.
            return False
        return super().available

    @property
    def native_value(self) -> int | None:
        wan = self.coordinator.data.wan_link
        return wan.speed_mbps if wan else None


class IptimeMeshStationCountSensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity
):
    """How many EasyMesh satellite stations are currently reporting in.

    None (not 0) when EasyMesh isn't active on this router at all, so a
    router that has never used mesh reads differently from one where mesh
    is on but every satellite has dropped off.
    """

    _attr_name = "ipTIME 이지메시 위성 기기 수"
    _attr_native_unit_of_measurement = "대"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:access-point-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "mesh_station_count")

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            self.coordinator.last_update_success
            and (not data.mesh_enabled or data.mesh_topology_available)
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return len(data.mesh_clients) if data.mesh_enabled else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            "mesh_enabled": data.mesh_enabled,
            "topology_available": data.mesh_topology_available,
        }
