from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

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
        IptimeNetworkDiagnosticsSensor(coordinator, entry),
        IptimeMeshDiagnosticsSensor(coordinator, entry),
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


class IptimeNetworkDiagnosticsSensor(_IptimeBaseSensor):
    """A compact snapshot suitable for HA dashboards and history."""

    _attr_name = "ipTIME 네트워크 상태"
    _attr_icon = "mdi:router-network"

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "network_diagnostics")

    @property
    def native_value(self) -> str:
        snapshot = self.coordinator.data
        data = snapshot.diagnostics
        if not data:
            return "JSON 진단 불가"
        if snapshot.diagnostics_updated_at and time.time() - snapshot.diagnostics_updated_at > 600:
            return "진단 갱신 실패"
        wan_config = data.get("wan_config")
        if (
            isinstance(wan_config, dict) and wan_config.get("enable") is False
        ) or data.get("nat") is False or data.get("port_role") == "lan":
            return "AP/허브 모드"
        wan = data.get("wan")
        ports = data.get("ports")
        if isinstance(ports, list):
            wan_ports = [p for p in ports if isinstance(p, dict) and p.get("type") == "wan"]
            if wan_ports and all(p.get("link") in (None, "", "0", 0, False) for p in wan_ports):
                return "WAN 케이블 끊김"
        if isinstance(wan, dict) and wan.get("ip"):
            return "WAN IP 연결"
        return "WAN 상태 확인 필요"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        diag = data.diagnostics
        system = diag.get("system") if isinstance(diag.get("system"), dict) else {}
        wan = diag.get("wan") if isinstance(diag.get("wan"), dict) else {}
        dns = diag.get("dns") if isinstance(diag.get("dns"), dict) else {}
        firmware = diag.get("firmware") if isinstance(diag.get("firmware"), dict) else {}
        ports = diag.get("ports") if isinstance(diag.get("ports"), list) else []
        return {
            "source": "json_api" if diag else "legacy_only",
            "last_success": datetime.fromtimestamp(data.diagnostics_updated_at, timezone.utc).isoformat()
            if data.diagnostics_updated_at else None,
            "router_uptime_seconds": system.get("uptime"),
            "wan_ip": wan.get("ip"),
            "wan_mac": wan.get("mac"),
            "wan_gateway": wan.get("gateway"),
            "dns": dns,
            "firmware": firmware.get("version"),
            "wan_config_enabled": diag.get("wan_config", {}).get("enable")
            if isinstance(diag.get("wan_config"), dict) else None,
            "nat_enabled": diag.get("nat") if isinstance(diag.get("nat"), bool) else None,
            "ports": [
                {"type": p.get("type"), "port": p.get("port"), "link": p.get("link")}
                for p in ports if isinstance(p, dict)
            ],
        }


class IptimeMeshDiagnosticsSensor(_IptimeBaseSensor):
    _attr_name = "ipTIME EasyMesh 상태"
    _attr_icon = "mdi:access-point-network"

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "mesh_diagnostics")

    @property
    def native_value(self) -> str:
        snapshot = self.coordinator.data
        if snapshot.diagnostics_updated_at and time.time() - snapshot.diagnostics_updated_at > 600:
            return "진단 갱신 실패"
        mesh = snapshot.diagnostics.get("mesh")
        if not isinstance(mesh, dict):
            return "정보 없음"
        if mesh.get("active") is False:
            return "비활성"
        return str(mesh.get("role") or mesh.get("current_role") or "활성")

    @property
    def extra_state_attributes(self) -> dict:
        diag = self.coordinator.data.diagnostics
        mesh = diag.get("mesh") if isinstance(diag.get("mesh"), dict) else {}
        raw = diag.get("mesh_agents")
        agents = raw.get("agents", raw.get("agent", [])) if isinstance(raw, dict) else raw
        if not isinstance(agents, list):
            agents = []
        return {
            "controller_mac": mesh.get("controller_mac"),
            "agents": [
                {k: agent.get(k) for k in ("mac", "al_mac", "nickname", "product_name", "status", "backhaul", "connection")}
                for agent in agents if isinstance(agent, dict)
            ],
        }
