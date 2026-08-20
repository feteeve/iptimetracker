from __future__ import annotations

import html as html_lib
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

from .const import CONF_RSSI_LIMIT, DEFAULT_RSSI_LIMIT, DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class IptimeAuthenticationError(UpdateFailed):
    """Raised when the router rejects the administrator credentials."""


class IptimeCaptchaRequired(UpdateFailed):
    """Raised when interactive CAPTCHA authentication is enabled."""


@dataclass
class WirelessClient:
    mac: str
    ip: str
    hostname: str
    interface: str
    rssi: int | None = None


@dataclass
class DhcpLease:
    mac: str
    ip: str
    hostname: str
    expires: str | None = None


@dataclass
class StaticLease:
    mac: str
    ip: str
    hostname: str


@dataclass
class IptimeData:
    connected_clients: list[WirelessClient] = field(default_factory=list)
    wireless_clients: list[WirelessClient] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    static_leases: list[StaticLease] = field(default_factory=list)


class IptimeClient:
    """HTTP client for the current, classic and mobile (IUX) ipTIME admin UIs."""

    # Two known classic login handlers; some firmware only serves one of them.
    LEGACY_LOGIN_PATHS = ("/sess-bin/login_handler.cgi", "/login/login.cgi")
    LEGACY_DATA_PATH = "/sess-bin/timepro.cgi"
    BETA_UI_PATH = "/ui/"
    BETA_SERVICE_PATH = "/cgi/service.cgi"
    HOSTINFO_PATH = "/login/hostinfo2.cgi"
    MOBILE_LOGIN_PATH = "/m_handler.cgi"
    MOBILE_WLAN_PATHS = {
        "2.4GHz": "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=2g&bssidx=0",
        "5GHz": "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=5g&bssidx=65536",
    }
    MESH_LEGACY_PATH = "/sess-bin/timepro.cgi?tmenu=wirelessconf&smenu=easymesh"
    MESH_MOBILE_PATH = "/cgi/iux_get.cgi?tmenu=sysconf&smenu=info&act=status"
    MESH_STATION_PATH = "/easymesh/api.cgi?key=topology"

    _MAC_PATTERN = re.compile(r"([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})")
    _IP_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
    _RSSI_PATTERN = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)
    _ROW_PATTERN = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
    _CELL_PATTERN = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
    _TAG_PATTERN = re.compile(r"<[^>]+>")

    def __init__(self, host: str, username: str, password: str) -> None:
        host = host.strip().rstrip("/")
        if not urlsplit(host).scheme:
            host = f"http://{host}"
        self._base_url = host
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._api_mode: str | None = None  # "beta" | "legacy" | "mobile"
        self._logged_in = False
        self._mesh_enabled = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                # Routers normally use an IP address. aiohttp's safe cookie jar
                # intentionally rejects cookies from IP hosts.
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                headers={
                    "User-Agent": "Mozilla/5.0 (Home Assistant ipTIME Tracker)",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
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
                    raise UpdateFailed(f"ipTIME HTTP 오류 {response.status} ({path})")
                return response, text
        except (aiohttp.ClientError, TimeoutError) as err:
            self._logged_in = False
            raise UpdateFailed(f"ipTIME 연결 실패 ({path}): {err}") from err

    async def _supports_beta_ui(self) -> bool:
        for path in (f"{self.BETA_UI_PATH}flutter_bootstrap.js", self.BETA_UI_PATH):
            try:
                _, text = await self._request_text("GET", path)
            except UpdateFailed as err:
                _LOGGER.debug("ipTIME 신형 UI 확인 실패(%s): %s", path, err)
                continue
            if self.BETA_SERVICE_PATH in text or "flutter" in text.lower():
                return True
        _LOGGER.debug("ipTIME 신형(Beta) UI 미감지, 다음 UI 확인")
        return False

    async def _supports_mobile_ui(self) -> bool:
        try:
            _, text = await self._request_text("GET", self.HOSTINFO_PATH)
        except UpdateFailed as err:
            _LOGGER.debug("ipTIME 모바일 UI 확인 실패(%s): %s", self.HOSTINFO_PATH, err)
            return False
        found = "iux" in text.lower()
        if not found:
            _LOGGER.debug("ipTIME 모바일(IUX) UI 미감지, 구형 UI로 진행")
        return found

    async def login(self) -> bool:
        """Detect the router UI (beta / mobile IUX / classic) and create a session.

        Each `_login_*` helper either returns True or raises a specific
        IptimeCaptchaRequired / IptimeAuthenticationError / UpdateFailed, so
        the caller always learns exactly why a login attempt failed instead
        of a generic False.
        """
        self._logged_in = False
        self._mesh_enabled = False

        if await self._supports_beta_ui():
            self._api_mode = "beta"
            await self._login_beta()
            _LOGGER.info("ipTIME 로그인 성공: 신형(Beta) UI (%s)", self._base_url)
        elif await self._supports_mobile_ui():
            self._api_mode = "mobile"
            await self._login_mobile()
            _LOGGER.info("ipTIME 로그인 성공: 모바일(IUX) UI (%s)", self._base_url)
        else:
            self._api_mode = "legacy"
            await self._login_legacy()
            _LOGGER.info("ipTIME 로그인 성공: 구형 UI (%s)", self._base_url)

        self._mesh_enabled = await self._detect_mesh_enabled()
        if self._mesh_enabled:
            _LOGGER.info("ipTIME EasyMesh 감지됨: 위성 기기도 함께 추적합니다")
        return True

    async def _login_beta(self) -> bool:
        response, payload = await self._request_json(
            "session/login", {"id": self._username, "pw": self._password}
        )
        self._logged_in = bool(payload.get("result"))
        if not self._logged_in:
            error = payload.get("error") or {}
            if error.get("code") == -31997:
                raise IptimeCaptchaRequired("ipTIME 관리자 CAPTCHA 인증이 활성화되어 있습니다")
            if error.get("code") == -31996:
                raise IptimeAuthenticationError("ipTIME 관리자 계정이 올바르지 않습니다")
            raise UpdateFailed(f"ipTIME 로그인 API 오류: {error}")

        session = await self._get_session()
        stored_cookie = session.cookie_jar.filter_cookies(response.url).get(
            "efm_session_id"
        )
        if "efm_session_id" not in response.cookies and not stored_cookie:
            raise UpdateFailed("ipTIME 로그인 응답에 세션 쿠키가 없습니다")
        return True

    async def _login_legacy(self) -> bool:
        """Try each known classic login handler; firmware only serves one of them."""
        auth_error: IptimeAuthenticationError | None = None
        last_error: UpdateFailed | None = None
        for path in self.LEGACY_LOGIN_PATHS:
            try:
                if await self._attempt_legacy_login(path):
                    return True
            except IptimeCaptchaRequired:
                raise
            except IptimeAuthenticationError as err:
                auth_error = err
            except UpdateFailed as err:
                # Keep the real reason (e.g. a connection failure) instead of
                # silently discarding it in favor of a generic fallback below.
                _LOGGER.debug("ipTIME 구형 로그인(%s) 실패: %s", path, err)
                last_error = err
        if auth_error:
            raise auth_error
        if last_error:
            raise last_error
        raise UpdateFailed("ipTIME 로그인 응답에서 세션을 찾지 못했습니다")

    async def _attempt_legacy_login(self, path: str) -> bool:
        response, text = await self._request_text(
            "POST",
            path,
            data={"username": self._username, "passwd": self._password},
            allow_redirects=True,
        )
        session_cookie = response.cookies.get("efm_session_id")
        session = await self._get_session()
        stored_cookie = session.cookie_jar.filter_cookies(response.url).get(
            "efm_session_id"
        )
        session_in_body = re.search(r"\b([A-Za-z0-9]{16})\b", text)
        if "captcha" in text.lower() and "captcha_on" not in text.lower():
            raise IptimeCaptchaRequired("ipTIME 관리자 CAPTCHA 인증이 활성화되어 있습니다")
        login_failed = (
            "login_session.cgi?noauto=1" in text
            or 'name="passwd"' in text
            or "efm_pwchk" in text
        )
        if login_failed:
            raise IptimeAuthenticationError("ipTIME 관리자 계정이 올바르지 않습니다")
        self._logged_in = bool(session_cookie or stored_cookie or session_in_body)
        if self._logged_in and not session_cookie and not stored_cookie and session_in_body:
            session.cookie_jar.update_cookies(
                {"efm_session_id": session_in_body.group(1)}, response.url
            )
        return self._logged_in

    async def _login_mobile(self) -> bool:
        response, text = await self._request_text(
            "POST",
            self.MOBILE_LOGIN_PATH,
            data={"username": self._username, "passwd": self._password},
        )
        session = await self._get_session()
        stored_cookie = session.cookie_jar.filter_cookies(response.url).get(
            "efm_session_id"
        )
        session_in_body = re.search(r"\b([A-Za-z0-9]{16})\b", text)
        if not stored_cookie and session_in_body:
            session.cookie_jar.update_cookies(
                {"efm_session_id": session_in_body.group(1)}, response.url
            )
            stored_cookie = session_in_body.group(1)
        self._logged_in = bool(stored_cookie or session_in_body)
        if not self._logged_in:
            raise IptimeAuthenticationError(
                "ipTIME(모바일) 로그인 응답에서 세션을 찾지 못했습니다. 계정을 확인하세요"
            )
        return True

    async def _request_json(
        self,
        method_name: str,
        params: dict[str, Any] | None = None,
        *,
        retry_on_auth: bool = True,
    ) -> tuple[aiohttp.ClientResponse, dict[str, Any]]:
        response, text = await self._request_text(
            "POST",
            self.BETA_SERVICE_PATH,
            json={"method": method_name, **({"params": params} if params else {})},
        )
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise UpdateFailed(f"ipTIME JSON 응답 해석 실패 ({method_name})") from err
        if not isinstance(payload, dict):
            raise UpdateFailed(f"ipTIME JSON 응답 형식 오류 ({method_name})")

        # Some firmware reports an expired session with codes/messages other than
        # -31998, so also match on the error text itself and transparently retry
        # once after a fresh login (mirrors the community iptime_manager project).
        if retry_on_auth and self._looks_unauthorized(payload):
            _LOGGER.debug("ipTIME 세션 만료 감지(%s), 재로그인 후 재시도", method_name)
            self._logged_in = False
            if await self._login_beta():
                return await self._request_json(method_name, params, retry_on_auth=False)
        return response, payload

    @staticmethod
    def _looks_unauthorized(payload: dict[str, Any]) -> bool:
        error = payload.get("error")
        if not error:
            return False
        code = error.get("code")
        message = str(error.get("message") or "").lower()
        return code == -31998 or any(word in message for word in ("session", "login", "auth"))

    async def get_wireless_clients(self) -> list[WirelessClient]:
        if self._api_mode == "beta":
            return await self._get_beta_clients()
        if self._api_mode == "mobile":
            return await self._get_mobile_clients()
        return await self._get_legacy_clients()

    async def _get_beta_clients(self) -> list[WirelessClient]:
        _, payload = await self._request_json("network/interface/lan/stations")
        result = payload.get("result")
        if result is None:
            error = payload.get("error") or {}
            if error.get("code") == -31998:
                self._logged_in = False
                raise UpdateFailed("ipTIME 세션 만료")
            raise UpdateFailed(f"ipTIME 접속자 조회 실패: {error}")

        clients: list[WirelessClient] = []
        for device in result:
            connection = device.get("connection") or {}
            connection_type = str(connection.get("type") or "unknown")
            details = connection.get(connection_type) or {}
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
            clients.append(
                WirelessClient(
                    mac=self._normalize_mac(str(device.get("mac", ""))),
                    ip=str(info.get("ip") or ""),
                    hostname=str(
                        info.get("name")
                        or info.get("hostname")
                        or device.get("mac")
                    ),
                    interface=interface,
                    rssi=rssi,
                )
            )
        return [client for client in clients if client.mac]

    async def _get_mobile_clients(self) -> list[WirelessClient]:
        clients: dict[str, WirelessClient] = {}
        successful_requests = 0
        for interface, path in self.MOBILE_WLAN_PATHS.items():
            try:
                _, text = await self._request_text("GET", path)
            except UpdateFailed as err:
                _LOGGER.debug("ipTIME(모바일) %s 조회 실패: %s", interface, err)
                continue
            try:
                payload = json.loads(text)
            except ValueError as err:
                _LOGGER.debug("ipTIME(모바일) %s 응답 파싱 실패: %s", interface, err)
                continue
            successful_requests += 1
            for device in payload.get("stalist") or []:
                mac = self._normalize_mac(str(device.get("mac", "")))
                if not mac:
                    continue
                clients[mac] = WirelessClient(
                    mac=mac,
                    ip=str(device.get("ipaddr") or ""),
                    hostname=str(device.get("hostname") or device.get("name") or mac),
                    interface=interface,
                    rssi=self._as_int(device.get("rssi")),
                )
        # A router with zero connected devices is not a failure; only raise
        # when every band request itself failed (matches _get_legacy_clients).
        if not successful_requests:
            raise UpdateFailed("ipTIME(모바일) 무선 접속자 페이지를 조회할 수 없습니다")
        return list(clients.values())

    async def _detect_mesh_enabled(self) -> bool:
        """Whether this router has EasyMesh active (a topology query is only useful then)."""
        try:
            if self._api_mode == "beta":
                _, payload = await self._request_json("easymesh/info", retry_on_auth=False)
                result = payload.get("result") or {}
                return bool(result.get("active"))
            if self._api_mode == "mobile":
                _, text = await self._request_text("GET", self.MESH_MOBILE_PATH)
                return "easymesh" in json.loads(text)
            _, text = await self._request_text("GET", self.MESH_LEGACY_PATH)
            match = re.search(
                r'<input\b[^>]*id="mode_none"[^>]*>', text, re.IGNORECASE | re.DOTALL
            )
            return bool(match and "checked" not in match.group(0).lower())
        except (UpdateFailed, ValueError) as err:
            _LOGGER.debug("ipTIME EasyMesh 감지 실패(정상: 메쉬 미사용 공유기일 수 있음): %s", err)
            return False

    async def get_mesh_clients(self, rssi_limit: int) -> list[WirelessClient]:
        """Devices connected to EasyMesh satellite units, filtered by RSSI."""
        if not self._mesh_enabled:
            return []
        try:
            _, text = await self._request_text("GET", self.MESH_STATION_PATH)
            data = json.loads(text)
        except (UpdateFailed, ValueError) as err:
            _LOGGER.debug("ipTIME EasyMesh 위성기기 조회 실패: %s", err)
            return []

        clients: list[WirelessClient] = []
        for station in data.get("station") or []:
            if not isinstance(station, dict):
                continue
            mac = self._normalize_mac(str(station.get("mac") or ""))
            if not mac:
                continue
            if str(station.get("connection") or "").strip() in {"Unknown", "WIRED"}:
                continue
            rssi = self._as_int(station.get("rssi"))
            if rssi is not None and rssi < rssi_limit:
                continue
            clients.append(
                WirelessClient(
                    mac=mac,
                    ip=str(station.get("ip") or ""),
                    hostname=str(station.get("name") or mac),
                    interface=f"메쉬-{station.get('mode') or 'unknown'}",
                    rssi=rssi,
                )
            )
        return clients

    async def _get_legacy_clients(self) -> list[WirelessClient]:
        clients: dict[str, WirelessClient] = {}
        successful_requests = 0
        for bssidx, interface in (("0", "2.4GHz"), ("65536", "5GHz")):
            try:
                _, page = await self._request_text(
                    "GET",
                    self.LEGACY_DATA_PATH,
                    params={
                        "tmenu": "iframe",
                        "smenu": "macauth_pcinfo_status",
                        "bssidx": bssidx,
                    },
                )
                if self._is_login_page(page):
                    self._logged_in = False
                    raise UpdateFailed("ipTIME 세션 만료")
                successful_requests += 1
                for client in self._parse_wireless(page, interface):
                    clients[client.mac] = client
            except UpdateFailed as err:
                if "세션 만료" in str(err):
                    raise
                _LOGGER.debug("ipTIME %s 조회 실패: %s", interface, err)

        if not successful_requests:
            raise UpdateFailed("ipTIME 무선 접속자 페이지를 조회할 수 없습니다")
        return list(clients.values())

    def _parse_wireless(self, page: str, interface: str) -> list[WirelessClient]:
        clients: list[WirelessClient] = []
        for row in self._ROW_PATTERN.finditer(page):
            row_text = row.group()
            mac_match = self._MAC_PATTERN.search(row_text)
            if not mac_match:
                continue
            cells = [
                self._clean_cell(match.group(1))
                for match in self._CELL_PATTERN.finditer(row_text)
            ]
            ip = next(
                (
                    match.group()
                    for cell in cells
                    if (match := self._IP_PATTERN.search(cell))
                ),
                "",
            )
            rssi_match = self._RSSI_PATTERN.search(" ".join(cells))
            hostname = next(
                (
                    cell
                    for cell in reversed(cells)
                    if cell
                    and not self._MAC_PATTERN.search(cell)
                    and not self._IP_PATTERN.search(cell)
                    and not self._RSSI_PATTERN.search(cell)
                    and not re.fullmatch(r"\d+", cell)
                ),
                "",
            )
            mac = self._normalize_mac(mac_match.group(1))
            clients.append(
                WirelessClient(
                    mac=mac,
                    ip=ip,
                    hostname=hostname or mac,
                    interface=interface,
                    rssi=int(rssi_match.group(1)) if rssi_match else None,
                )
            )
        return clients

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        try:
            _, page = await self._request_text(
                "GET",
                self.LEGACY_DATA_PATH,
                params={"tmenu": "iframe", "smenu": "lan_pcinfo_status"},
            )
        except UpdateFailed:
            return []
        if self._is_login_page(page):
            return []
        return self._parse_dhcp_leases(page)

    def _parse_dhcp_leases(self, page: str) -> list[DhcpLease]:
        leases: list[DhcpLease] = []
        for row in self._ROW_PATTERN.finditer(page):
            row_text = row.group()
            mac_match = self._MAC_PATTERN.search(row_text)
            ip_match = self._IP_PATTERN.search(row_text)
            if not mac_match or not ip_match:
                continue
            cells = [
                self._clean_cell(match.group(1))
                for match in self._CELL_PATTERN.finditer(row_text)
            ]
            mac = self._normalize_mac(mac_match.group(1))
            hostname = next(
                (
                    cell
                    for cell in cells
                    if cell
                    and not self._MAC_PATTERN.search(cell)
                    and not self._IP_PATTERN.search(cell)
                ),
                mac,
            )
            leases.append(DhcpLease(mac=mac, ip=ip_match.group(1), hostname=hostname))
        return leases

    async def get_static_leases(self) -> list[StaticLease]:
        return []

    async def fetch_all(self, rssi_limit: int = DEFAULT_RSSI_LIMIT) -> IptimeData:
        if not self._logged_in and not await self.login():
            raise UpdateFailed("ipTIME 로그인 실패: 계정 또는 CAPTCHA 설정을 확인하세요")

        try:
            wireless = await self.get_wireless_clients()
        except UpdateFailed as err:
            if "세션 만료" not in str(err):
                raise
            _LOGGER.info("ipTIME 세션 만료 감지, 재로그인 후 재조회합니다")
            await self.login()  # raises with the specific reason if this fails too
            wireless = await self.get_wireless_clients()

        dhcp = await self.get_dhcp_leases() if self._api_mode == "legacy" else []
        mesh = await self.get_mesh_clients(rssi_limit) if self._mesh_enabled else []
        connected = {client.mac: client for client in wireless}
        # The classic UI exposes wired clients through lan_pcinfo_status and
        # wireless clients through separate per-band pages.
        for lease in dhcp:
            connected.setdefault(
                lease.mac,
                WirelessClient(
                    mac=lease.mac,
                    ip=lease.ip,
                    hostname=lease.hostname,
                    interface="LAN/unknown",
                ),
            )
        # EasyMesh satellite stations aren't visible to the main router's own
        # client list, so they're merged in (and take priority: they carry RSSI).
        for client in mesh:
            connected[client.mac] = client
        connected_clients = list(connected.values())
        wireless_clients = [
            client
            for client in connected_clients
            if client.interface not in {"wired", "ethernet", "LAN/unknown"}
        ]
        _LOGGER.debug(
            "ipTIME update (%s): %d connected clients (%d wireless, %d mesh)",
            self._api_mode,
            len(connected_clients),
            len(wireless_clients),
            len(mesh),
        )
        return IptimeData(
            connected_clients=connected_clients,
            wireless_clients=wireless_clients,
            dhcp_leases=dhcp,
        )

    @classmethod
    def _clean_cell(cls, value: str) -> str:
        return " ".join(html_lib.unescape(cls._TAG_PATTERN.sub("", value)).split())

    @classmethod
    def _normalize_mac(cls, value: str) -> str:
        match = cls._MAC_PATTERN.search(value)
        return match.group(1).upper().replace("-", ":") if match else ""

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_login_page(page: str) -> bool:
        return (
            'name="passwd"' in page
            or "login_session.cgi?noauto=1" in page
            or "efm_pwchk" in page
        )


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
        rssi_limit = self.entry.options.get(
            CONF_RSSI_LIMIT, self.entry.data.get(CONF_RSSI_LIMIT, DEFAULT_RSSI_LIMIT)
        )
        return await self.client.fetch_all(rssi_limit=rssi_limit)
