from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DEFAULT_HOST, DEFAULT_USERNAME, DOMAIN
from .coordinator import (
    IptimeAuthenticationError,
    IptimeCaptchaRequired,
    IptimeClient,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class IptimeTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()

            client = IptimeClient(
                host=user_input[CONF_HOST],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                logged_in = await client.login()
                if not logged_in:
                    errors["base"] = "invalid_auth"
                else:
                    await client.close()
                    return self.async_create_entry(
                        title=f"ipTIME ({user_input[CONF_HOST]})",
                        data=user_input,
                    )
            except IptimeCaptchaRequired:
                errors["base"] = "captcha_required"
            except IptimeAuthenticationError:
                errors["base"] = "invalid_auth"
            except UpdateFailed as err:
                _LOGGER.error("ipTIME connection validation failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error while validating ipTIME connection")
                errors["base"] = "unknown"
            finally:
                await client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
