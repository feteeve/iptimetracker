from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

try:
    from homeassistant.components.ssdp import SsdpServiceInfo
except ImportError:  # pragma: no cover - depends on the HA version installed
    SsdpServiceInfo = Any  # type: ignore[assignment,misc]

from .const import (
    CONF_CONSIDER_HOME,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_RSSI_LIMIT,
    CONF_USERNAME,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_HOST,
    DEFAULT_RSSI_LIMIT,
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


def _user_schema(*, default_host: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_CONSIDER_HOME, default=DEFAULT_CONSIDER_HOME): int,
            vol.Optional(CONF_RSSI_LIMIT, default=DEFAULT_RSSI_LIMIT): int,
        }
    )


class IptimeTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

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

        host = urlsplit(url).netloc or url
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()
        self._discovered_host = host
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        error_detail = ""

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()

            client = IptimeClient(
                host=user_input[CONF_HOST],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                await client.close()
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
                "error_detail": f"\n\n오류 상세: {error_detail}" if error_detail else ""
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IptimeTrackerOptionsFlow:
        return IptimeTrackerOptionsFlow(config_entry)


class IptimeTrackerOptionsFlow(OptionsFlow):
    """Let the user tune presence-detection behavior after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_consider_home = self._config_entry.options.get(
            CONF_CONSIDER_HOME,
            self._config_entry.data.get(CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME),
        )
        current_rssi_limit = self._config_entry.options.get(
            CONF_RSSI_LIMIT,
            self._config_entry.data.get(CONF_RSSI_LIMIT, DEFAULT_RSSI_LIMIT),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CONSIDER_HOME, default=current_consider_home
                    ): int,
                    vol.Optional(CONF_RSSI_LIMIT, default=current_rssi_limit): int,
                }
            ),
        )
