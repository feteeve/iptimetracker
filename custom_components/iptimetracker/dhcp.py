"""DHCP lease display and guarded reservation creation for the JSON UI."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", value.strip())
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        raise ValueError("올바른 MAC 주소가 아닙니다")
    if int(compact, 16) == 0 or int(compact[:2], 16) & 1:
        raise ValueError("단일 기기의 MAC 주소가 필요합니다")
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2)).upper()


def parse_rows(value: Any, *, reservation: bool) -> list[dict[str, str]] | None:
    """Return a normalized list, or None if this firmware's shape is unknown."""
    if not isinstance(value, list):
        return None
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        try:
            mac = normalize_mac(str(item.get("mac") or ""))
            ip = str(ipaddress.IPv4Address(item.get("ip")))
        except (ValueError, ipaddress.AddressValueError):
            return None
        row = {
            "mac": mac,
            "ip": ip,
            "name": str(item.get("desc" if reservation else "hostname") or item.get("name") or ""),
        }
        if not reservation:
            row["expires"] = str(item.get("expires") or item.get("expire") or "")
        rows.append(row)
    return rows


class DhcpReservations:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.lock = asyncio.Lock()

    async def _read(self, method: str, params: Any = None) -> Any:
        _, payload = await self.client._request_json(method, params)
        if payload.get("error") is not None or payload.get("result") is None:
            raise ValueError("공유기의 DHCP 조회에 실패했습니다")
        return payload["result"]

    async def list(self) -> list[dict[str, str]]:
        """Read current reservations when the user opens the add flow."""
        rows = parse_rows(await self._read("dhcpd/reservedaddr/show", "lan"), reservation=True)
        if rows is None:
            raise ValueError("수동 할당 목록 형식을 확인할 수 없습니다")
        return rows

    async def add(self, mac_value: str, ip_value: str, description: str = "") -> list[dict[str, str]]:
        """Create one reservation after fresh conflict checks and readback."""
        mac = normalize_mac(mac_value)
        ip = str(ipaddress.IPv4Address(ip_value))
        if len(description) > 100 or any(ord(char) < 32 for char in description):
            raise ValueError("설명은 제어 문자가 없는 100자 이하여야 합니다")
        async with self.lock:
            existing = parse_rows(await self._read("dhcpd/reservedaddr/show", "lan"), reservation=True)
            leases = parse_rows(await self._read("dhcpd/lease/show", "lan"), reservation=False)
            if existing is None or leases is None:
                raise ValueError("DHCP 목록 형식을 확인할 수 없어 변경을 중단했습니다")
            if any(row["mac"] == mac for row in existing):
                raise ValueError("이 MAC은 이미 수동 할당되어 있습니다")
            if any(row["ip"] == ip and row["mac"] != mac for row in existing + leases):
                raise ValueError("이 IP는 다른 기기에 할당되었거나 예약되어 있습니다")
            lan = await self._read("network/interface/lan/info")
            if not isinstance(lan, dict) or not lan.get("ip") or not lan.get("mask"):
                raise ValueError("LAN 대역을 확인할 수 없어 변경을 중단했습니다")
            subnet = ipaddress.IPv4Network(f"{lan['ip']}/{lan['mask']}", strict=False)
            address = ipaddress.IPv4Address(ip)
            if address not in subnet or address in (
                subnet.network_address,
                subnet.broadcast_address,
                ipaddress.IPv4Address(lan["ip"]),
            ):
                raise ValueError("공유기 LAN에서 사용할 수 있는 IP가 아닙니다")
            stations = await self._read("network/interface/lan/stations")
            if not isinstance(stations, list):
                raise ValueError("현재 접속 기기 목록을 확인할 수 없습니다")
            for station in stations:
                if not isinstance(station, dict):
                    raise ValueError("접속 기기 목록 형식이 올바르지 않습니다")
                info = station.get("info")
                station_ip = info.get("ip") if isinstance(info, dict) else station.get("ip")
                if station_ip == ip and normalize_mac(str(station.get("mac") or "")) != mac:
                    raise ValueError("이 IP를 다른 접속 기기가 사용 중입니다")
            # Do not retry a write on timeout: it may already have applied.
            _, result = await self.client._request_json(
                "dhcpd/reservedaddr/add",
                {"ntag": "lan", "mac": mac, "ip": ip, "desc": description},
                retry_on_auth=False,
            )
            if result.get("error") is not None or result.get("result") is None:
                raise ValueError("공유기가 수동 할당 요청을 거부했습니다")
            try:
                updated = parse_rows(await self._read("dhcpd/reservedaddr/show", "lan"), reservation=True)
            except Exception as err:
                raise ValueError("수동 할당 요청 후 재조회에 실패했습니다. 다시 시도하기 전에 공유기 목록을 확인하세요") from err
            if updated is None or not any(row["mac"] == mac and row["ip"] == ip for row in updated):
                raise ValueError("적용 여부를 확인하지 못했습니다. 다시 시도하기 전에 공유기 목록을 확인하세요")
            return updated
