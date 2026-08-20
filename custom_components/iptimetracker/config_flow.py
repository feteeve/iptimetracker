from __future__ import annotations

import logging
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

try:
    from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
except ImportError:  # pragma: no cover - depends on the HA version installed
    from homeassistant.components.ssdp import SsdpServiceInfo

from .const import (
    CONF_CONSIDER_HOME,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_RSSI_LIMIT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_HOST,
    DEFAULT_RSSI_LIMIT,
    DEFAULT_SCAN_INTERVAL,
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

CONSIDER_HOME_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=0, max=86400))
RSSI_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=-120, max=0))
SCAN_INTERVAL_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=10, max=3600))


def _user_schema(*, default_host: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(
                CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
            ): SCAN_INTERVAL_VALIDATOR,
            vol.Optional(
                CONF_CONSIDER_HOME, default=DEFAULT_CONSIDER_HOME
            ): CONSIDER_HOME_VALIDATOR,
            vol.Optional(
                CONF_RSSI_LIMIT, default=DEFAULT_RSSI_LIMIT
            ): RSSI_VALIDATOR,
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
    """Let the user tune presence-detection behavior after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep compatibility with HA releases predating OptionsFlow.config_entry.
        self._provided_config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        config_entry = getattr(self, "config_entry", self._provided_config_entry)
        current_scan_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL,
            config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        current_consider_home = config_entry.options.get(
            CONF_CONSIDER_HOME,
            config_entry.data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
        )
        current_rssi_limit = config_entry.options.get(
            CONF_RSSI_LIMIT,
            config_entry.data.get(CONF_RSSI_LIMIT, DEFAULT_RSSI_LIMIT),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current_scan_interval
                    ): SCAN_INTERVAL_VALIDATOR,
                    vol.Optional(
                        CONF_CONSIDER_HOME, default=current_consider_home
                    ): CONSIDER_HOME_VALIDATOR,
                    vol.Optional(
                        CONF_RSSI_LIMIT, default=current_rssi_limit
                    ): RSSI_VALIDATOR,
                }
            ),
        )
