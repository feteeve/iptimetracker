from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

try:
    from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
except ImportError:  # pragma: no cover - depends on the HA version installed
    from homeassistant.components.ssdp import SsdpServiceInfo

from .const import (
    CONF_DEVICE_NICKNAMES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_TRACKED_MACS,
    CONF_USERNAME,
    DEFAULT_HOST,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .coordinator import (
    IptimeAuthenticationError,
    IptimeCaptchaRequired,
    IptimeClient,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)


def _ip_sort_key(ip: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in ip.split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)
    return parts if len(parts) == 4 else (999, 999, 999, 999)


def _collect_device_info(data: object, currently_tracked: set[str]) -> dict[str, dict[str, str]]:
    """Gather per-MAC display info, keeping reported and reservation names distinct.

    Sourced from live connected clients and named static-IP reservations,
    sorted by IP. A previously selected device that isn't in either list
    right now is kept (as offline) so picking it again doesn't require it
    to reconnect first. Shared by the device picker and the nickname step
    so both agree on what the router currently calls each device.
    """
    entries: dict[str, dict[str, str]] = {}
    if data is not None:
        for client in data.connected_clients:
            if not client.mac:
                continue
            entries[client.mac] = {
                "ip": client.ip,
                "device_name": (
                    client.hostname
                    if client.hostname and client.hostname != client.mac
                    else ""
                ),
                "reservation_name": client.reservation_name or "",
                "static_ip": "",
                "reservation_confidence": client.reservation_name_confidence or "",
            }
        for lease in data.static_leases:
            if not lease.mac:
                continue
            entry = entries.setdefault(
                lease.mac,
                {
                    "ip": lease.ip,
                    "device_name": "",
                    "reservation_name": "",
                    "static_ip": lease.ip,
                    "reservation_confidence": lease.name_confidence,
                },
            )
            entry["static_ip"] = lease.ip
            if lease.hostname and lease.hostname != lease.mac:
                entry["reservation_name"] = lease.hostname
                entry["reservation_confidence"] = lease.name_confidence
    for mac in currently_tracked:
        entries.setdefault(
            mac,
            {
                "ip": "",
                "device_name": "",
                "reservation_name": "",
                "static_ip": "",
                "reservation_confidence": "",
            },
        )
    return dict(sorted(entries.items(), key=lambda item: _ip_sort_key(item[1]["ip"])))


def _suggested_name(info: dict[str, str]) -> str:
    """The router-reported name to pre-fill a nickname field with, if any."""
    return info["reservation_name"] or info["device_name"] or ""


_EMPTY_DEVICE_INFO: dict[str, str] = {
    "ip": "",
    "device_name": "",
    "reservation_name": "",
    "static_ip": "",
    "reservation_confidence": "",
}
_NICKNAME_FIELD_PREFIX = "nickname::"
_REMOVE_FIELD_PREFIX = "remove::"


def _nickname_field(mac: str) -> str:
    return f"{_NICKNAME_FIELD_PREFIX}{mac}"


def _remove_field(mac: str) -> str:
    return f"{_REMOVE_FIELD_PREFIX}{mac}"


def _duplicate_nickname(nicknames: dict[str, str]) -> str | None:
    """First nickname value used by more than one device, if any (blanks ignored).

    Two devices sharing a nickname would otherwise collide on the same
    entity_id/display name and get silently disambiguated by Home Assistant
    (e.g. an auto-appended "_2") - better to catch it before saving.
    """
    seen: set[str] = set()
    for name in nicknames.values():
        if not name:
            continue
        if name in seen:
            return name
        seen.add(name)
    return None


def _build_device_choices(entries: dict[str, dict[str, str]]) -> dict[str, str]:
    choices: dict[str, str] = {}
    for mac, info in entries.items():
        device_name = info["device_name"]
        reservation_name = info["reservation_name"]
        reservation_label = (
            f"{reservation_name} (추정)"
            if reservation_name and info["reservation_confidence"] == "low"
            else reservation_name
        )
        if reservation_name and device_name and reservation_name != device_name:
            names = f"등록명: {reservation_label} / 기기명: {device_name}"
        elif reservation_name:
            names = f"등록명: {reservation_label}"
        elif device_name:
            names = f"기기명: {device_name}"
        else:
            names = "이름 없음"
        static_suffix = (
            f" / 고정 IP: {info['static_ip']}" if info["static_ip"] else ""
        )
        choices[mac] = f"{info['ip'] or '?'} - {mac}\n{names}{static_suffix}"
    return choices


def _user_schema(*, default_host: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


class IptimeTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._discovered_host: str | None = None

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Handle a router discovered automatically over SSDP."""
        upnp = getattr(discovery_info, "upnp", {}) or {}
        url = upnp.get("presentationURL")
        if not url:
            location = getattr(discovery_info, "ssdp_location", None)
            if location:
                parsed = urlsplit(location)
                url = f"{parsed.scheme}://{parsed.netloc}"
        if not url:
            return self.async_abort(reason="cannot_connect")

        try:
            normalized = IptimeClient.normalize_host(url)
        except ValueError:
            return self.async_abort(reason="cannot_connect")
        host = urlsplit(normalized).netloc
        if self._host_already_configured(normalized):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(normalized)
        self._abort_if_unique_id_configured()
        self._discovered_host = host
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        error_detail = ""

        if user_input is not None:
            try:
                normalized_host = IptimeClient.normalize_host(user_input[CONF_HOST])
            except ValueError as err:
                errors["base"] = "cannot_connect"
                error_detail = str(err)
                normalized_host = ""

            if normalized_host:
                if self._host_already_configured(normalized_host):
                    return self.async_abort(reason="already_configured")
                user_input = {**user_input, CONF_HOST: normalized_host}
                await self.async_set_unique_id(normalized_host)
                self._abort_if_unique_id_configured()

            if not normalized_host:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(
                        default_host=self._discovered_host or DEFAULT_HOST
                    ),
                    errors=errors,
                    description_placeholders={
                        "error_detail": self._error_detail(error_detail)
                    },
                )

            client = IptimeClient(
                host=user_input[CONF_HOST],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                # Device selection happens later from the integration's own
                # settings (⚙️), which can query the router live at any
                # time - keep initial setup to just the credentials.
                return self.async_create_entry(
                    title=f"ipTIME ({user_input[CONF_HOST]})",
                    data=user_input,
                )
            except IptimeCaptchaRequired as err:
                errors["base"] = "captcha_required"
                error_detail = str(err)
            except IptimeAuthenticationError as err:
                errors["base"] = "invalid_auth"
                error_detail = str(err)
            except UpdateFailed as err:
                _LOGGER.error("ipTIME connection validation failed: %s", err)
                errors["base"] = "cannot_connect"
                error_detail = str(err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error while validating ipTIME connection")
                errors["base"] = "unknown"
                error_detail = f"{type(err).__name__}: {err}"
            finally:
                await client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(default_host=self._discovered_host or DEFAULT_HOST),
            errors=errors,
            # Always show the router's own failure reason, not just the
            # generic error key, so a bad setup can be diagnosed on the spot.
            description_placeholders={
                "error_detail": self._error_detail(error_detail)
            },
        )

    def _error_detail(self, detail: str) -> str:
        if not detail:
            return ""
        language = getattr(self.hass.config, "language", "en")
        prefix = "오류 상세" if language.lower().startswith("ko") else "Error details"
        return f"\n\n{prefix}: {detail}"

    def _host_already_configured(self, normalized_host: str) -> bool:
        """Detect entries created before host normalization was introduced."""
        for entry in self._async_current_entries():
            try:
                configured_host = IptimeClient.normalize_host(entry.data[CONF_HOST])
            except (KeyError, TypeError, ValueError):
                continue
            if configured_host == normalized_host:
                return True
        return False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IptimeTrackerOptionsFlow:
        return IptimeTrackerOptionsFlow(config_entry)


class IptimeTrackerOptionsFlow(OptionsFlow):
    """Pick which routed devices get a device_tracker entity.

    The single "init" screen doubles as both: a picker (multi-select, choices
    limited to devices *not* already tracked) for adding new devices, and a
    list of already-tracked devices with an editable nickname field and a
    "remove" checkbox each - removal is explicit there, not implied by
    unchecking a box, so a device already being tracked never disappears by
    accident. Adding new devices continues into a "nicknames" step (since
    which boxes got checked isn't known until this form is submitted).
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep compatibility with HA releases predating OptionsFlow.config_entry.
        self._provided_config_entry = config_entry
        self._selected_macs: list[str] = []
        self._pending_tracked_macs: list[str] = []
        self._pending_nicknames: dict[str, str] = {}

    @property
    def _entry(self) -> ConfigEntry:
        return getattr(self, "config_entry", self._provided_config_entry)

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        tracked_macs = list(self._entry.options.get(CONF_TRACKED_MACS, []))
        current_nicknames: dict[str, str] = self._entry.options.get(
            CONF_DEVICE_NICKNAMES, {}
        )

        if user_input is not None:
            kept_macs = [
                mac for mac in tracked_macs if not user_input.get(_remove_field(mac))
            ]
            updated_nicknames = {
                mac: nickname
                for mac in kept_macs
                if (nickname := user_input.get(_nickname_field(mac), "").strip())
            }
            duplicate = _duplicate_nickname(updated_nicknames)
            if duplicate:
                return await self._show_init_form(
                    tracked_macs,
                    current_nicknames,
                    errors={"base": "duplicate_nickname"},
                    error_placeholders={"nickname": duplicate},
                    field_overrides=user_input,
                )

            newly_selected = [
                mac
                for mac in user_input.get(CONF_TRACKED_MACS, [])
                if mac not in kept_macs
            ]
            if newly_selected:
                self._pending_tracked_macs = kept_macs
                self._pending_nicknames = updated_nicknames
                self._selected_macs = newly_selected
                return await self.async_step_nicknames()

            return self.async_create_entry(
                title="",
                data={
                    CONF_TRACKED_MACS: kept_macs,
                    CONF_DEVICE_NICKNAMES: updated_nicknames,
                },
            )

        return await self._show_init_form(tracked_macs, current_nicknames)

    async def _show_init_form(
        self,
        tracked_macs: list[str],
        current_nicknames: dict[str, str],
        *,
        errors: dict[str, str] | None = None,
        error_placeholders: dict[str, str] | None = None,
        field_overrides: dict[str, object] | None = None,
    ) -> ConfigFlowResult:
        """`field_overrides` re-applies a just-rejected submission's raw values
        (e.g. after a duplicate-nickname error) so the user doesn't have to
        retype everything."""
        tracked_set = set(tracked_macs)
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        device_info = _collect_device_info(
            coordinator.data if coordinator else None, tracked_set
        )
        addable_choices = {
            mac: label
            for mac, label in _build_device_choices(device_info).items()
            if mac not in tracked_set
        }

        schema_dict: dict[Any, Any] = {
            vol.Optional(
                CONF_TRACKED_MACS,
                default=(field_overrides or {}).get(CONF_TRACKED_MACS, []),
            ): cv.multi_select(addable_choices),
        }
        tracked_lines = []
        for mac in tracked_macs:
            info = device_info.get(mac, _EMPTY_DEVICE_INFO)
            nickname = current_nicknames.get(mac, "")
            nickname_default = (
                (field_overrides or {}).get(_nickname_field(mac), nickname)
                if field_overrides
                else nickname
            )
            remove_default = bool(
                (field_overrides or {}).get(_remove_field(mac), False)
            )
            schema_dict[
                vol.Optional(_nickname_field(mac), default=nickname_default)
            ] = str
            schema_dict[vol.Optional(_remove_field(mac), default=remove_default)] = bool
            device_name = _suggested_name(info) or "이름 없음"
            tracked_lines.append(
                f"{info['ip'] or '?'} - {mac} (기기명: {device_name}, "
                f"닉네임: {nickname or '-'})"
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            errors=errors or {},
            description_placeholders={
                "tracked_list": "\n".join(tracked_lines) if tracked_lines else "없음",
                **(error_placeholders or {}),
            },
        )

    async def async_step_nicknames(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Name the devices just newly selected on the init screen.

        Pre-filled with whatever the router currently reports, left blank
        when nothing is known yet - the field is optional, so leaving it
        blank keeps using the live router-reported name instead.
        """
        if user_input is not None:
            new_nicknames = {
                mac: name.strip() for mac, name in user_input.items() if name.strip()
            }
            merged_nicknames = {**self._pending_nicknames, **new_nicknames}
            duplicate = _duplicate_nickname(merged_nicknames)
            if duplicate:
                return await self._show_nicknames_form(
                    errors={"base": "duplicate_nickname"},
                    error_placeholders={"nickname": duplicate},
                    field_overrides=user_input,
                )
            return self.async_create_entry(
                title="",
                data={
                    CONF_TRACKED_MACS: self._pending_tracked_macs + self._selected_macs,
                    CONF_DEVICE_NICKNAMES: merged_nicknames,
                },
            )

        return await self._show_nicknames_form()

    async def _show_nicknames_form(
        self,
        *,
        errors: dict[str, str] | None = None,
        error_placeholders: dict[str, str] | None = None,
        field_overrides: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        device_info = _collect_device_info(
            coordinator.data if coordinator else None, set(self._selected_macs)
        )

        schema_dict: dict[Any, type[str]] = {}
        device_lines = []
        for mac in self._selected_macs:
            info = device_info.get(mac, _EMPTY_DEVICE_INFO)
            default = (field_overrides or {}).get(mac, _suggested_name(info))
            schema_dict[vol.Optional(mac, default=default)] = str
            device_lines.append(f"{info['ip'] or '?'} - {mac}")

        return self.async_show_form(
            step_id="nicknames",
            data_schema=vol.Schema(schema_dict),
            errors=errors or {},
            description_placeholders={
                "device_list": "\n".join(device_lines),
                **(error_placeholders or {}),
            },
        )
