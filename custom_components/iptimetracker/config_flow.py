from __future__ import annotations

import logging
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


def _build_device_choices(data: object, currently_tracked: set[str]) -> dict[str, str]:
    """List known devices while keeping reported and reservation names distinct.

    Sourced from live connected clients and named static-IP reservations,
    sorted by IP. A previously selected device that isn't in either list
    right now is kept (as offline) so picking it again doesn't require it
    to reconnect first.
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

    choices: dict[str, str] = {}
    ordered = sorted(entries.items(), key=lambda item: _ip_sort_key(item[1]["ip"]))
    for mac, info in ordered:
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

    This is the only options step: opening the integration's settings goes
    straight to the device picker, built from what the router reports right
    now (connected clients and named static-IP reservations) instead of
    asking the user to type MAC addresses blind.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep compatibility with HA releases predating OptionsFlow.config_entry.
        self._provided_config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        config_entry = getattr(self, "config_entry", self._provided_config_entry)

        if user_input is not None:
            return self.async_create_entry(
                title="", data={CONF_TRACKED_MACS: user_input[CONF_TRACKED_MACS]}
            )

        currently_tracked = set(config_entry.options.get(CONF_TRACKED_MACS, []))
        coordinator = self.hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
        choices = _build_device_choices(
            coordinator.data if coordinator else None, currently_tracked
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TRACKED_MACS,
                        default=[mac for mac in currently_tracked if mac in choices],
                    ): cv.multi_select(choices)
                }
            ),
        )
