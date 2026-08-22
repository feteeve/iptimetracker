from __future__ import annotations

import logging
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
_DONE_SENTINEL = "__done__"


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
        normalized = name.casefold()
        if normalized in seen:
            return name
        seen.add(normalized)
    return None


def _reported_name_label(info: dict[str, str]) -> str:
    """A single, human-friendly name for a device, preferring the router's
    named static-IP reservation over the name it reports while connected."""
    device_name = info["device_name"]
    reservation_name = info["reservation_name"]
    if not reservation_name and not device_name:
        return "이름 없음"
    reservation_label = (
        f"{reservation_name} (추정)"
        if reservation_name and info["reservation_confidence"] == "low"
        else reservation_name
    )
    if reservation_name and device_name and reservation_name != device_name:
        return f"{reservation_label} (기기명: {device_name})"
    return reservation_label or device_name


def _addable_device_label(mac: str, info: dict[str, str]) -> str:
    """One-line, scannable label for the "pick a device to add" selector."""
    static_suffix = f" · 고정 IP" if info["static_ip"] else ""
    return f"{info['ip'] or '?'} · {_reported_name_label(info)}{static_suffix} · {mac}"


def _build_device_choices(entries: dict[str, dict[str, str]]) -> dict[str, str]:
    return {mac: _addable_device_label(mac, info) for mac, info in entries.items()}


def _tracked_device_label(mac: str, info: dict[str, str], nickname: str) -> str:
    """One-line label identifying an already-tracked device by its current
    nickname (falling back to whatever the router reports)."""
    name = nickname or _reported_name_label(info)
    return f"{name} · {info['ip'] or '?'} · {mac}"


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
    """Add devices to track, or rename/remove ones already tracked.

    The entry screen is a two-item menu rather than one big form - adding
    devices and managing existing ones are different tasks, and cramming
    both into a single form (as earlier versions did) meant a wall of
    per-device fields with no real labels. Each task then walks through one
    screen at a time - one device to name, one device to edit - so every
    field keeps a plain, translated label, and the device it's currently
    about (IP/MAC/router-reported name) is shown as context text instead of
    being baked into the field itself.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Keep compatibility with HA releases predating OptionsFlow.config_entry.
        self._provided_config_entry = config_entry
        self._selected_macs: list[str] = []
        self._add_index = 0
        self._new_nicknames: dict[str, str] = {}
        self._pending_tracked_macs: list[str] | None = None
        self._pending_nicknames: dict[str, str] = {}
        self._editing_mac: str | None = None

    @property
    def _entry(self) -> ConfigEntry:
        return getattr(self, "config_entry", self._provided_config_entry)

    def _device_info(self, tracked: set[str]) -> dict[str, dict[str, str]]:
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        return _collect_device_info(coordinator.data if coordinator else None, tracked)

    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        tracked_count = len(self._entry.options.get(CONF_TRACKED_MACS, []))
        menu_options = ["add_select"]
        if tracked_count:
            menu_options.append("manage_select")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={"tracked_count": str(tracked_count)},
        )

    # ---- Add devices --------------------------------------------------

    async def async_step_add_select(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Pick one or more currently-untracked devices to start tracking."""
        tracked_set = set(self._entry.options.get(CONF_TRACKED_MACS, []))
        device_info = self._device_info(tracked_set)
        addable_choices = {
            mac: label
            for mac, label in _build_device_choices(device_info).items()
            if mac not in tracked_set
        }

        if not addable_choices:
            return self.async_abort(reason="no_addable_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_TRACKED_MACS, [])
            if not selected:
                errors["base"] = "select_at_least_one"
            else:
                self._selected_macs = selected
                self._add_index = 0
                self._new_nicknames = {}
                return await self.async_step_add_nickname()

        return self.async_show_form(
            step_id="add_select",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACKED_MACS, default=[]): cv.multi_select(
                        addable_choices
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_add_nickname(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Name one just-selected device at a time.

        Pre-filled with whatever the router currently reports, left blank
        when nothing is known yet - the field is optional, so leaving it
        blank keeps using the live router-reported name instead.
        """
        mac = self._selected_macs[self._add_index]
        current_nicknames: dict[str, str] = self._entry.options.get(
            CONF_DEVICE_NICKNAMES, {}
        )
        errors: dict[str, str] = {}
        error_placeholders: dict[str, str] = {}

        if user_input is not None:
            nickname = user_input.get("nickname", "").strip()
            others = {
                other_mac: name
                for other_mac, name in {**current_nicknames, **self._new_nicknames}.items()
                if other_mac != mac
            }
            duplicate = _duplicate_nickname({**others, mac: nickname})
            if duplicate:
                errors["base"] = "duplicate_nickname"
                error_placeholders["nickname"] = duplicate
            else:
                if nickname:
                    self._new_nicknames[mac] = nickname
                self._add_index += 1
                if self._add_index < len(self._selected_macs):
                    return await self.async_step_add_nickname()

                tracked_macs = list(self._entry.options.get(CONF_TRACKED_MACS, []))
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_TRACKED_MACS: tracked_macs + self._selected_macs,
                        CONF_DEVICE_NICKNAMES: {
                            **current_nicknames,
                            **self._new_nicknames,
                        },
                    },
                )

        device_info = self._device_info(set(self._selected_macs))
        info = device_info.get(mac, _EMPTY_DEVICE_INFO)
        return self.async_show_form(
            step_id="add_nickname",
            data_schema=vol.Schema(
                {vol.Optional("nickname", default=_suggested_name(info)): str}
            ),
            errors=errors,
            description_placeholders={
                "position": str(self._add_index + 1),
                "total": str(len(self._selected_macs)),
                "ip": info["ip"] or "?",
                "mac": mac,
                "reported_name": _reported_name_label(info),
                **error_placeholders,
            },
        )

    # ---- Manage existing devices ---------------------------------------

    def _ensure_pending_state(self) -> None:
        if self._pending_tracked_macs is None:
            self._pending_tracked_macs = list(
                self._entry.options.get(CONF_TRACKED_MACS, [])
            )
            self._pending_nicknames = dict(
                self._entry.options.get(CONF_DEVICE_NICKNAMES, {})
            )

    async def async_step_manage_select(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Pick one tracked device to rename or stop tracking, or finish up.

        Edits accumulate here across as many devices as the user wants to
        touch, and are only written to the config entry once "완료" is
        picked - so closing the dialog partway through discards changes
        instead of silently saving a half-finished edit.
        """
        self._ensure_pending_state()
        tracked_macs = self._pending_tracked_macs
        assert tracked_macs is not None

        if user_input is not None:
            device = user_input["device"]
            if device == _DONE_SENTINEL:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_TRACKED_MACS: tracked_macs,
                        CONF_DEVICE_NICKNAMES: self._pending_nicknames,
                    },
                )
            self._editing_mac = device
            return await self.async_step_edit_device()

        device_info = self._device_info(set(tracked_macs))
        options = [
            SelectOptionDict(value=_DONE_SENTINEL, label="✅ 완료 (변경사항 저장)")
        ]
        for mac in tracked_macs:
            info = device_info.get(mac, _EMPTY_DEVICE_INFO)
            nickname = self._pending_nicknames.get(mac, "")
            options.append(
                SelectOptionDict(
                    value=mac, label=_tracked_device_label(mac, info, nickname)
                )
            )

        return self.async_show_form(
            step_id="manage_select",
            data_schema=vol.Schema(
                {
                    vol.Required("device", default=_DONE_SENTINEL): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
            description_placeholders={"tracked_count": str(len(tracked_macs))},
        )

    async def async_step_edit_device(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Rename or remove the single device picked on the previous screen."""
        tracked_macs = self._pending_tracked_macs
        mac = self._editing_mac
        assert tracked_macs is not None and mac is not None
        errors: dict[str, str] = {}
        error_placeholders: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove"):
                tracked_macs.remove(mac)
                self._pending_nicknames.pop(mac, None)
                return await self.async_step_manage_select()

            nickname = user_input.get("nickname", "").strip()
            others = {
                other_mac: name
                for other_mac, name in self._pending_nicknames.items()
                if other_mac != mac
            }
            duplicate = _duplicate_nickname({**others, mac: nickname})
            if duplicate:
                errors["base"] = "duplicate_nickname"
                error_placeholders["nickname"] = duplicate
            else:
                if nickname:
                    self._pending_nicknames[mac] = nickname
                else:
                    self._pending_nicknames.pop(mac, None)
                return await self.async_step_manage_select()

        device_info = self._device_info(set(tracked_macs))
        info = device_info.get(mac, _EMPTY_DEVICE_INFO)
        current_nickname = self._pending_nicknames.get(mac, "")
        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema(
                {
                    vol.Optional("nickname", default=current_nickname): str,
                    vol.Optional("remove", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "ip": info["ip"] or "?",
                "mac": mac,
                "reported_name": _reported_name_label(info),
                **error_placeholders,
            },
        )
