"""Compact, credential-free views of the manually collected router snapshot."""

from __future__ import annotations

from typing import Any
import re


def link_label(value: Any) -> str:
    """Translate router link notation into a readable connection speed."""
    if value is None or str(value).strip().lower() in {"", "null", "down", "0"}:
        return "끊김/미확인"
    match = re.fullmatch(r"(\d+)([fh])?", str(value).strip().lower())
    if not match:
        return "확인 불가"
    speed = int(match.group(1))
    unit = f"{speed / 1000:g} Gbps" if speed >= 1000 else f"{speed} Mbps"
    duplex = {"f": " · 전이중", "h": " · 반이중"}.get(match.group(2), "")
    return unit + duplex


def ports(snapshot: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Merge link state and sampled traffic by physical port identity."""
    raw = snapshot.get("raw")
    links = raw.get("port.links") if isinstance(raw, dict) else None
    traffic = snapshot.get("traffic")
    rates = traffic.get("ports") if isinstance(traffic, dict) else None
    if not isinstance(links, list) and not isinstance(rates, list):
        return None
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in links if isinstance(links, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        number = str(item.get("port")) if item.get("port") is not None else ""
        if not kind:
            continue
        merged[(kind, number)] = {"type": kind, "port": number, "link": item.get("link")}
    for item in rates if isinstance(rates, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        number = str(item.get("port")) if item.get("port") is not None else ""
        if not kind:
            continue
        row = merged.setdefault((kind, number), {"type": kind, "port": number})
        for key in ("link", "rx_mbps", "tx_mbps", "rx_drop_delta", "rx_crc_delta", "tx_collision_delta"):
            if key in item:
                row[key] = item[key]
    for row in merged.values():
        row["link_label"] = link_label(row.get("link"))
    return list(merged.values())


def connected_stations(snapshot: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Show observed connection details without interpreting DHCP leases as online."""
    raw = snapshot.get("raw")
    stations = raw.get("network.lan_stations") if isinstance(raw, dict) else None
    if not isinstance(stations, list):
        return None
    result: list[dict[str, Any]] = []
    for item in stations:
        if not isinstance(item, dict):
            continue
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        connection = item.get("connection") if isinstance(item.get("connection"), dict) else {}
        kind = str(connection.get("type") or "unknown")
        details = connection.get(kind) if isinstance(connection.get(kind), dict) else {}
        result.append({
            "mac": item.get("mac"),
            "ip": info.get("ip"),
            "name": info.get("name") or item.get("name"),
            "connection": kind,
            "band": details.get("bss") if kind == "wireless" else None,
            "rssi_dbm": details.get("rssi") if kind == "wireless" else None,
        })
    return result


def selected_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose only known safe keys from LAN, DHCP and Wi-Fi configuration."""
    raw = snapshot.get("raw")
    if not isinstance(raw, dict):
        return {}
    groups = {
        "lan": ("network.lan_info", ("ip", "mask", "gateway")),
        "dhcp": ("dhcp.config", ("active", "enabled", "start", "end", "lease_time", "lease")),
        "dhcp_status": ("dhcp.status", ("active", "enabled", "status")),
        "wireless_channel": ("wireless.channel_config", ("band", "channel", "width", "bandwidth")),
    }
    result: dict[str, Any] = {}
    for label, (key, allowed) in groups.items():
        value = raw.get(key)
        if isinstance(value, dict):
            filtered = {field: value[field] for field in allowed if field in value and isinstance(value[field], (str, int, float, bool))}
            if filtered:
                result[label] = filtered
    return result
