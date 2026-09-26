from __future__ import annotations

import logging
from datetime import datetime, timezone

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
            IptimeNetworkDiagnosticsSensor(coordinator, entry),
            IptimeMeshDiagnosticsSensor(coordinator, entry),
            IptimeInformationCoverageSensor(coordinator, entry),
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


class IptimeNetworkDiagnosticsSensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity
):
    """WAN and system snapshot from supported read-only JSON methods."""

    _attr_name = "ipTIME 네트워크 진단"
    _attr_icon = "mdi:router-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "network_diagnostics")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if not data.diagnostics:
            return "정보 없음"
        wan = data.diagnostics.get("wan")
        if isinstance(wan, dict) and wan.get("ip"):
            return "WAN IP 할당됨"
        return "WAN 상태 확인 필요"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        diagnostics = data.diagnostics
        system = diagnostics.get("system") if isinstance(diagnostics.get("system"), dict) else {}
        wan = diagnostics.get("wan") if isinstance(diagnostics.get("wan"), dict) else {}
        dns = diagnostics.get("dns") if isinstance(diagnostics.get("dns"), dict) else {}
        firmware = diagnostics.get("firmware") if isinstance(diagnostics.get("firmware"), dict) else {}
        return {
            "last_success": datetime.fromtimestamp(data.diagnostics_updated_at, timezone.utc).isoformat()
            if data.diagnostics_updated_at else None,
            "router_uptime_seconds": system.get("uptime"),
            "wan_ip": wan.get("ip"),
            "wan_gateway": wan.get("gateway"),
            "wan_mac": wan.get("mac"),
            "dns": dns,
            "firmware": firmware.get("version"),
        }


class IptimeMeshDiagnosticsSensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity
):
    """EasyMesh role and satellite status, separate from station presence."""

    _attr_name = "ipTIME 이지메시 진단"
    _attr_icon = "mdi:access-point-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "mesh_diagnostics")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        mesh = data.diagnostics.get("mesh")
        if not isinstance(mesh, dict):
            return "정보 없음"
        if mesh.get("active") is False:
            return "비활성"
        return str(mesh.get("role") or mesh.get("current_role") or "활성")

    @property
    def extra_state_attributes(self) -> dict:
        diagnostics = self.coordinator.data.diagnostics
        mesh = diagnostics.get("mesh") if isinstance(diagnostics.get("mesh"), dict) else {}
        raw = diagnostics.get("mesh_agents")
        agents = raw.get("agents", raw.get("agent", [])) if isinstance(raw, dict) else raw
        if not isinstance(agents, list):
            agents = []
        return {
            "controller_mac": mesh.get("controller_mac"),
            "agents": [
                {key: agent.get(key) for key in
                 ("mac", "al_mac", "nickname", "product_name", "status", "backhaul", "connection")}
                for agent in agents if isinstance(agent, dict)
            ],
        }


class IptimeInformationCoverageSensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], SensorEntity
):
    """Number and categories of read-only APIs supported by this router."""

    _attr_name = "ipTIME 정보 수집 범위"
    _attr_icon = "mdi:database-search"
    _attr_native_unit_of_measurement = "개"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "information_coverage")

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data.diagnostics.get("supported_methods"))

    @property
    def native_value(self) -> int:
        methods = self.coordinator.data.diagnostics.get("supported_methods")
        return len(methods) if isinstance(methods, list) else 0

    @property
    def extra_state_attributes(self) -> dict:
        raw = self.coordinator.data.diagnostics.get("raw")
        if not isinstance(raw, dict):
            return {"collected": 0, "categories": {}}
        categories: dict[str, int] = {}
        for key in raw:
            category = str(key).split(".", 1)[0]
            categories[category] = categories.get(category, 0) + 1
        return {
            "collected": len(raw),
            "categories": categories,
        }
