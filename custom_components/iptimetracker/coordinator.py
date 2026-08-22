from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, RSSI_LIMIT, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class IptimeAuthenticationError(UpdateFailed):
    """Raised when the router rejects the administrator credentials."""


class IptimeCaptchaRequired(UpdateFailed):
    """Raised when interactive CAPTCHA authentication is enabled."""


class IptimeSessionExpired(UpdateFailed):
    """Raised when an authenticated router page redirects to login."""


@dataclass
class WirelessClient:
    mac: str
    ip: str
    hostname: str
    interface: str
    rssi: int | None = None
    reservation_name: str | None = None
    hostname_source: str | None = None
    reservation_name_source: str | None = None
    reservation_name_confidence: str | None = None

    @property
    def display_name(self) -> str:
        """Prefer the administrator-assigned reservation name for display."""
        return self.reservation_name or self.hostname or self.mac


@dataclass
class DhcpLease:
    mac: str
    ip: str
    hostname: str
    expires: str | None = None
    name_source: str = "dhcp"


@dataclass
class StaticLease:
    mac: str
    ip: str
    hostname: str
    name_source: str = "static_reservation"
    name_confidence: str = "high"


@dataclass
class WanLinkStatus:
    """Physical link state of the router's WAN (internet) port.

    Off/no-link here points at the ISP/modem side, independent of anything
    happening on Wi-Fi/LAN - useful for telling "the internet is down" apart
    from "one specific device has a problem".
    """

    connected: bool
    speed_mbps: int | None = None
    duplex: str | None = None
    raw: str | None = None


@dataclass
class IptimeData:
    connected_clients: list[WirelessClient] = field(default_factory=list)
    router_clients: list[WirelessClient] = field(default_factory=list)
    wireless_clients: list[WirelessClient] = field(default_factory=list)
    # The router's JSON admin API has no confirmed endpoint for DHCP leases
    # or static-IP reservations yet, so these stay empty for now rather than
    # scraping the old HTML admin pages this firmware doesn't actually serve.
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    static_leases: list[StaticLease] = field(default_factory=list)
    wan_link: WanLinkStatus | None = None
    mesh_enabled: bool = False
    mesh_clients: list[WirelessClient] = field(default_factory=list)
    mesh_topology_available: bool = True


class IptimeClient:
    """HTTP client for the current ipTIME admin UI's JSON-RPC API."""

    UI_PATH = "/ui/"
    SERVICE_PATH = "/cgi/service.cgi"
    MESH_STATION_PATH = "/easymesh/api.cgi?key=topology"
    WAN_LINK_METHOD = "port/link/status"

    _LINK_SPEED_PATTERN = re.compile(r"^(\d+)([fh]?)$")

    _MAC_PATTERN = re.compile(r"([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})")
    _IP_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
    _RSSI_PATTERN = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)
    _NAME_FIELD_CANDIDATES = (
        "name",
        "hostname",
        "host_name",
        "device_name",
        "pc_name",
        "alias",
        "nickname",
        "label",
        "comment",
        "description",
    )
    _NON_NAME_VALUES = {
        "유선",
        "무선",
        "wired",
        "wireless",
        "삭제",
        "등록",
        "추가",
        "선택",
        "delete",
        "add",
        "apply",
        "수동할당",
        "수동 할당",
        "자동할당",
        "자동 할당",
        "manual",
        "static",
        "dynamic",
    }

    def __init__(self, host: str, username: str, password: str) -> None:
        self._base_url = self.normalize_host(host)
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False
        self._mesh_enabled = False
        self._last_mesh_clients: list[WirelessClient] = []

    _SCHEME_PREFIX_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

    @classmethod
    def normalize_host(cls, host: str) -> str:
        """Return a stable router origin used by HTTP and config unique IDs.

        Checking for a literal "scheme://" prefix (rather than trusting
        urlsplit's bare `.scheme`) matters because urlsplit misreads a
        hostname:port with no scheme, e.g. "router.local:8080", as
        scheme="router.local" / path="8080" — an IP like "192.168.0.1:8080"
        happens to dodge this since a scheme can't start with a digit, but a
        DNS hostname does not, and normalize_host would otherwise wrongly
        reject a perfectly valid address.
        """
        value = host.strip()
        if not cls._SCHEME_PREFIX_PATTERN.match(value):
            value = f"http://{value}"
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("공유기 주소는 올바른 HTTP 또는 HTTPS 주소여야 합니다")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                # Routers normally use an IP address. aiohttp's safe cookie jar
                # intentionally rejects cookies from IP hosts.
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                headers={
                    # The admin UI's /cgi/service.cgi enforces an
                    # Origin/Referer check on its API and silently drops (no
                    # response at all, not even an error) requests that don't
                    # look like they came from its own page - confirmed by
                    # comparing against the community iptime_manager project,
                    # which sends a real browser User-Agent plus Origin/Referer
                    # on this exact endpoint. Without them, login just hangs
                    # until timeout instead of failing fast.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                    "Origin": self._base_url,
                    "Referer": f"{self._base_url}/",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request_text(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[aiohttp.ClientResponse, str]:
        session = await self._get_session()
        try:
            async with session.request(
                method,
                f"{self._base_url}{path}",
                timeout=aiohttp.ClientTimeout(total=10),
                **kwargs,
            ) as response:
                text = await response.text(errors="replace")
                if response.status >= 400:
                    if response.status in (401, 403):
                        self._logged_in = False
                    raise UpdateFailed(f"ipTIME HTTP 오류 {response.status} ({path})")
                return response, text
        except (aiohttp.ClientError, TimeoutError) as err:
            self._logged_in = False
            raise UpdateFailed(f"ipTIME 연결 실패 ({path}): {err}") from err

    async def login(self) -> bool:
        """Log in via the router's JSON-RPC admin API and validate the session.

        One same-session retry absorbs a transient hiccup without escalating
        to anything else, since there's only one login path.
        """
        self._logged_in = False

        last_err: UpdateFailed | None = None
        for attempt in (1, 2):
            if attempt > 1:
                await asyncio.sleep(1)
            self._logged_in = False
            await self._clear_session_cookies()
            try:
                await self._login_request()
                # A login cookie alone is not enough: validate the client-list
                # API actually works too before declaring success.
                await self.get_wireless_clients()
                break
            except (IptimeCaptchaRequired, IptimeAuthenticationError):
                raise
            except UpdateFailed as err:
                last_err = err
                _LOGGER.debug("ipTIME 로그인 시도 %d/2 실패: %s", attempt, err)
        else:
            raise last_err or UpdateFailed("ipTIME 로그인에 실패했습니다")

        _LOGGER.info("ipTIME 로그인 성공 (%s)", self._base_url)
        return True

    async def _login_request(self) -> bool:
        response, payload = await self._request_json(
            "session/login",
            {"id": self._username, "pw": self._password},
            retry_on_auth=False,
        )
        self._logged_in = bool(payload.get("result"))
        if not self._logged_in:
            error = payload.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == -31997:
                raise IptimeCaptchaRequired("ipTIME 관리자 CAPTCHA 인증이 활성화되어 있습니다")
            if code == -31996:
                raise IptimeAuthenticationError("ipTIME 관리자 계정이 올바르지 않습니다")
            raise UpdateFailed(f"ipTIME 로그인 API 오류: {error}")

        session = await self._get_session()
        stored_cookie = session.cookie_jar.filter_cookies(response.url).get(
            "efm_session_id"
        )
        if "efm_session_id" not in response.cookies and not stored_cookie:
            raise UpdateFailed("ipTIME 로그인 응답에 세션 쿠키가 없습니다")
        return True

    async def _clear_session_cookies(self) -> None:
        session = await self._get_session()
        session.cookie_jar.clear()

    async def _request_json(
        self,
        method_name: str,
        params: dict[str, Any] | None = None,
        *,
        retry_on_auth: bool = True,
    ) -> tuple[aiohttp.ClientResponse, dict[str, Any]]:
        response, text = await self._request_text(
            "POST",
            self.SERVICE_PATH,
            json={"method": method_name, **({"params": params} if params else {})},
        )
        try:
            payload = json.loads(text)
        except ValueError as err:
            # Some firmware redirects an expired session to an HTML login
            # page instead of returning a JSON authentication error.
            if retry_on_auth:
                _LOGGER.debug(
                    "ipTIME non-JSON response for %s; refreshing session once",
                    method_name,
                )
                self._logged_in = False
                await self._login_request()
                return await self._request_json(
                    method_name, params, retry_on_auth=False
                )
            raise UpdateFailed(f"ipTIME JSON 응답 해석 실패 ({method_name})") from err
        if not isinstance(payload, dict):
            raise UpdateFailed(f"ipTIME JSON 응답 형식 오류 ({method_name})")

        # Some firmware reports an expired session with codes/messages other than
        # -31998, so also match on the error text itself and transparently retry
        # once after a fresh login (mirrors the community iptime_manager project).
        if retry_on_auth and self._looks_unauthorized(payload):
            _LOGGER.debug("ipTIME 세션 만료 감지(%s), 재로그인 후 재시도", method_name)
            self._logged_in = False
            if await self._login_request():
                return await self._request_json(method_name, params, retry_on_auth=False)
        return response, payload

    @staticmethod
    def _looks_unauthorized(payload: dict[str, Any]) -> bool:
        error = payload.get("error")
        if not error:
            return False
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or "").lower()
        else:
            code = None
            message = str(error).lower()
        return code == -31998 or any(
            word in message for word in ("session", "login", "auth")
        )

    async def get_wireless_clients(self) -> list[WirelessClient]:
        _, payload = await self._request_json("network/interface/lan/stations")
        result = payload.get("result")
        if result is None:
            error = payload.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == -31998:
                self._logged_in = False
                raise IptimeSessionExpired("ipTIME 세션이 만료되었습니다")
            raise UpdateFailed(f"ipTIME 접속자 조회 실패: {error}")
        if not isinstance(result, list):
            raise UpdateFailed(
                f"ipTIME 접속자 응답 형식 오류: result가 목록이 아님 ({type(result).__name__})"
            )

        clients: list[WirelessClient] = []
        for device in result:
            if not isinstance(device, dict):
                continue
            connection = device.get("connection") or {}
            if not isinstance(connection, dict):
                connection = {}
            connection_type = str(connection.get("type") or "unknown")
            details = connection.get(connection_type) or {}
            if not isinstance(details, dict):
                details = {}
            if connection_type == "wireless":
                bss = str(details.get("bss", "wireless"))
                interface = {
                    "2g.1": "2.4GHz",
                    "5g.1": "5GHz",
                    "6g.1": "6GHz",
                }.get(bss, bss)
                rssi = self._as_int(details.get("rssi"))
            else:
                interface = connection_type
                rssi = None
            info = device.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            mac = self._normalize_mac(str(device.get("mac", "")))
            name = self._name_from_mappings(mac, info, device)
            if not name and mac:
                _LOGGER.debug("ipTIME 기기 이름 필드 없음")
            clients.append(
                WirelessClient(
                    mac=mac,
                    ip=str(info.get("ip") or ""),
                    hostname=str(name or mac),
                    interface=interface,
                    rssi=rssi,
                    hostname_source="router_info" if name else None,
                )
            )
        self._log_json_name_diagnostics(source="stations", entries=result)
        return [client for client in clients if client.mac]

    async def _detect_mesh_enabled(self) -> bool | None:
        """Whether this router has EasyMesh active (a topology query is only useful then)."""
        try:
            _, payload = await self._request_json("easymesh/info")
            result = payload.get("result") or {}
            if not isinstance(result, dict) or "active" not in result:
                return None
            active = result["active"]
            if isinstance(active, bool):
                return active
            if isinstance(active, int):
                return active != 0
            normalized = str(active).strip().casefold()
            if normalized in {"1", "true", "yes", "on", "active", "enabled"}:
                return True
            if normalized in {
                "0",
                "false",
                "no",
                "off",
                "inactive",
                "disabled",
                "",
            }:
                return False
            return None
        except UpdateFailed as err:
            _LOGGER.debug("ipTIME EasyMesh 감지 실패(정상: 메쉬 미사용 공유기일 수 있음): %s", err)
            return None

    async def get_wan_link_status(self) -> WanLinkStatus | None:
        """Physical link state of the WAN port, confirmed against the
        community iptime_manager project's port/link/status usage."""
        try:
            _, payload = await self._request_json(self.WAN_LINK_METHOD, retry_on_auth=False)
        except UpdateFailed as err:
            # Temporary diagnostic: this call intentionally doesn't retry on
            # auth failure (to avoid a recursive re-login loop from an
            # ancillary status check), so an intermittent session hiccup
            # here shows up as the entity going "unavailable" for a poll
            # cycle. Logging the payload/error pins down whether that's what
            # is actually happening versus something else.
            _LOGGER.debug("ipTIME WAN 링크 상태 조회 실패: %s", err)
            return None
        result = payload.get("result")
        if not isinstance(result, list):
            _LOGGER.debug(
                "ipTIME WAN 링크 상태 응답 형식 이상: result=%r 전체 payload=%r",
                result,
                payload,
            )
            return None
        wan_port = next(
            (
                port for port in result
                if isinstance(port, dict) and str(port.get("type", "")).lower() == "wan"
            ),
            None,
        )
        if wan_port is None:
            _LOGGER.debug(
                "ipTIME WAN 링크 상태 응답에 WAN 타입 포트 없음: 받은 포트 목록=%r",
                result,
            )
            return None
        link = wan_port.get("link")
        if link in (None, "", "null"):
            _LOGGER.debug(
                "ipTIME WAN 링크 끊김으로 보고됨, 해당 포트 원본 데이터=%r", wan_port
            )
            return WanLinkStatus(connected=False)
        match = self._LINK_SPEED_PATTERN.match(str(link))
        if not match:
            return WanLinkStatus(connected=True, raw=str(link))
        return WanLinkStatus(
            connected=True,
            speed_mbps=int(match.group(1)),
            duplex={"f": "full", "h": "half"}.get(match.group(2)) or None,
            raw=str(link),
        )

    async def get_mesh_clients(self, rssi_limit: int) -> list[WirelessClient] | None:
        """Devices connected to EasyMesh satellite units, filtered by RSSI."""
        if not self._mesh_enabled:
            return []
        try:
            _, text = await self._request_text("GET", self.MESH_STATION_PATH)
            data = json.loads(text)
        except (UpdateFailed, ValueError) as err:
            _LOGGER.debug("ipTIME EasyMesh 위성기기 조회 실패: %s", err)
            return None

        if not isinstance(data, dict):
            _LOGGER.debug(
                "ipTIME EasyMesh topology response is %s, not an object",
                type(data).__name__,
            )
            return None
        stations = data.get("station") or []
        if not isinstance(stations, list):
            _LOGGER.debug("ipTIME EasyMesh station response is not a list")
            return None

        clients: list[WirelessClient] = []
        for station in stations:
            if not isinstance(station, dict):
                continue
            mac = self._normalize_mac(str(station.get("mac") or ""))
            if not mac:
                continue
            if str(station.get("connection") or "").strip().casefold() in {
                "unknown",
                "wired",
            }:
                continue
            rssi = self._as_int(station.get("rssi"))
            if rssi is not None and rssi < rssi_limit:
                continue
            station_name = self._name_from_mappings(mac, station)
            clients.append(
                WirelessClient(
                    mac=mac,
                    ip=str(station.get("ip") or ""),
                    hostname=station_name or mac,
                    interface=f"메쉬-{station.get('mode') or 'unknown'}",
                    rssi=rssi,
                    hostname_source=("easymesh_topology" if station_name else None),
                )
            )
        return clients

    @classmethod
    def _clean_name(cls, value: Any, mac: str) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            return ""
        lowered = text.casefold()
        if (
            cls._normalize_mac(text)
            or cls._valid_ip(text)
            or cls._RSSI_PATTERN.search(text)
            or re.fullmatch(r"[\d\s:./-]+", text)
            or lowered in cls._NON_NAME_VALUES
            or cls._normalize_mac(mac) == cls._normalize_mac(text)
        ):
            return ""
        return text

    @classmethod
    def _name_from_mappings(cls, mac: str, *mappings: Any) -> str:
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for field_name in cls._NAME_FIELD_CANDIDATES:
                if name := cls._clean_name(mapping.get(field_name), mac):
                    return name
        return ""

    @staticmethod
    def _log_json_name_diagnostics(*, source: str, entries: Any) -> None:
        """Record schema hints without logging names, IPs, MACs or payloads."""
        if not _LOGGER.isEnabledFor(logging.DEBUG) or not isinstance(entries, list):
            return
        device_keys: set[str] = set()
        info_keys: set[str] = set()
        for entry in entries[:10]:
            if not isinstance(entry, dict):
                continue
            device_keys.update(str(key) for key in entry)
            info = entry.get("info")
            if isinstance(info, dict):
                info_keys.update(str(key) for key in info)
        _LOGGER.debug(
            "ipTIME 이름 진단 source=%s entries=%d device_keys=%s info_keys=%s",
            source,
            len(entries),
            sorted(device_keys),
            sorted(info_keys),
        )

    async def fetch_all(self, rssi_limit: int = RSSI_LIMIT) -> IptimeData:
        if not self._logged_in and not await self.login():
            raise UpdateFailed("ipTIME 로그인 실패: 계정 또는 CAPTCHA 설정을 확인하세요")

        try:
            wireless = await self.get_wireless_clients()
        except IptimeSessionExpired:
            _LOGGER.info("ipTIME 세션 만료 감지, 재로그인 후 재조회합니다")
            await self.login()  # raises with the specific reason if this fails too
            wireless = await self.get_wireless_clients()

        detected_mesh = await self._detect_mesh_enabled()
        if detected_mesh is not None:
            if self._mesh_enabled and not detected_mesh:
                self._last_mesh_clients = []
            self._mesh_enabled = detected_mesh

        mesh_topology_available = True
        if self._mesh_enabled:
            mesh_result = await self.get_mesh_clients(rssi_limit)
            if mesh_result is None:
                # A failed topology request means "unknown", not "zero
                # stations". Retain the last good set to avoid false-away
                # transitions and expose the partial failure to entities.
                mesh_topology_available = False
                mesh = list(self._last_mesh_clients)
            else:
                mesh = mesh_result
                self._last_mesh_clients = list(mesh)
        else:
            mesh = []
            self._last_mesh_clients = []
        wan_link = await self.get_wan_link_status()
        connected = {client.mac: client for client in wireless}
        # EasyMesh satellite stations aren't visible to the main router's own
        # client list, so they're merged in (and take priority: they carry RSSI).
        for client in mesh:
            existing = connected.get(client.mac)
            if existing:
                if not client.ip:
                    client.ip = existing.ip
                if self._clean_name(client.hostname, client.mac) == "":
                    client.hostname = existing.hostname
                    client.hostname_source = existing.hostname_source
            connected[client.mac] = client
        connected_clients = list(connected.values())
        wireless_clients = [
            client
            for client in connected_clients
            if client.interface not in {"wired", "ethernet", "LAN/unknown"}
        ]
        _LOGGER.debug(
            "ipTIME update: %d connected clients (%d wireless, %d mesh)",
            len(connected_clients),
            len(wireless_clients),
            len(mesh),
        )
        return IptimeData(
            connected_clients=connected_clients,
            router_clients=wireless,
            wireless_clients=wireless_clients,
            dhcp_leases=[],
            static_leases=[],
            wan_link=wan_link,
            mesh_enabled=self._mesh_enabled,
            mesh_clients=mesh,
            mesh_topology_available=mesh_topology_available,
        )

    @classmethod
    def _normalize_mac(cls, value: str) -> str:
        match = cls._MAC_PATTERN.search(value)
        return match.group(1).upper().replace("-", ":") if match else ""

    @classmethod
    def _valid_ip(cls, value: str) -> str:
        match = cls._IP_PATTERN.search(value)
        if not match:
            return ""
        candidate = match.group(1)
        return (
            candidate if all(int(part) <= 255 for part in candidate.split(".")) else ""
        )

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class IptimeDataUpdateCoordinator(DataUpdateCoordinator[IptimeData]):
    """Coordinate periodic ipTIME client updates."""

    def __init__(
        self, hass: HomeAssistant, client: IptimeClient, entry: ConfigEntry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> IptimeData:
        return await self.client.fetch_all(rssi_limit=RSSI_LIMIT)
