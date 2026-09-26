from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import IptimeDataUpdateCoordinator

_REDACTED = "**REDACTED**"
_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "private_key",
    "preshared_key",
    "token",
    "captcha",
)
_SENSITIVE_EXACT = {"pw", "key", "psk", "wepkey", "wpakey"}


def _redact(value: Any) -> Any:
    """Recursively redact credentials that optional router APIs may return."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            result[str(key)] = (
                _REDACTED
                if normalized in _SENSITIVE_EXACT
                or any(part in normalized for part in _SENSITIVE_PARTS)
                else _redact(item)
            )
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the complete cached read-only router snapshot for diagnostics."""
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry_data = {
        key: (_REDACTED if "password" in key.casefold() else value)
        for key, value in entry.data.items()
    }
    return _redact(
        {
            "entry": entry_data,
            "last_update_success": coordinator.last_update_success,
            "router": asdict(coordinator.data),
        }
    )
