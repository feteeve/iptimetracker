from __future__ import annotations

import asyncio
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

from .const import (
    CONF_RSSI_LIMIT,
    CONF_SCAN_INTERVAL,
    DEFAULT_RSSI_LIMIT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

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

    # Known classic login handlers. The timepro form is required by a number of
    # older firmwares and was the protocol used by the first integration build.
    LEGACY_LOGIN_PATHS = ("/sess-bin/login_handler.cgi", "/login/login.cgi")
    LEGACY_DATA_PATH = "/sess-bin/timepro.cgi"
    BETA_UI_PATH = "/ui/"
    BETA_SERVICE_PATH = "/cgi/service.cgi"
    HOSTINFO_PATH = "/login/hostinfo2.cgi"
    MOBILE_LOGIN_PATH = "/m_handler.cgi"
    MOBILE_WLAN_PATHS = {
        "2.4GHz": (
            "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth"
            "&act=status&wlmode=2g&bssidx=0"
        ),
        "5GHz": (
            "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth"
            "&act=status&wlmode=5g&bssidx=65536"
        ),
    }
    MESH_LEGACY_PATH = (
        "/sess-bin/timepro.cgi?tmenu=wirelessconf&smenu=easymesh"
    )
    MESH_MOBILE_PATH = "/cgi/iux_get.cgi?tmenu=sysconf&smenu=info&act=status"
    MESH_STATION_PATH = "/easymesh/api.cgi?key=topology"

    _MAC_PATTERN = re.compile(r"([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})")
    _IP_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
    _RSSI_PATTERN = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)
    _ROW_PATTERN = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
    _CELL_PATTERN = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
    _TAG_PATTERN = re.compile(r"<[^>]+>")

    def __init__(self, host: str, username: str, password: str) -> None:
        self._base_url = self.normalize_host(host)
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._api_mode: str | None = None  # "beta" | "legacy" | "mobile"
        self._logged_in = False
        self._mesh_enabled = False

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
        lowered = text.lower()
        # hostinfo2.cgi commonly returns JavaScript assignments, but some
        # versions return JSON. Merely containing the word "iux" is not enough:
        # disabled/uninstalled flags are also present on classic-only routers.
        affirmative = re.search(
            r"(?:iux(?:_package_installed)?)\s*[:=]\s*[\"']?"
            r"(?:1|true|yes|on|installed)\b",
            lowered,
        )
        explicit_disabled = re.search(
            r"iux\s*[:=]\s*[\"']?(?:0|false|no|off|none)\b", lowered
        )
        found = bool(affirmative and not explicit_disabled)
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

        candidates: list[tuple[str, Any]] = []
        if await self._supports_beta_ui():
            candidates.append(("beta", self._login_beta))
        if await self._supports_mobile_ui():
            candidates.append(("mobile", self._login_mobile))
        # Always keep both classic protocols as fallbacks. UI feature probes
        # are hints, not proof that the corresponding login endpoint works.
        candidates.extend(
            (("original", self._login_original), ("legacy", self._login_legacy))
        )

        auth_error: IptimeAuthenticationError | None = None
        last_error: UpdateFailed | None = None
        for mode, login_method in candidates:
            try:
                if await self._try_login_candidate(mode, login_method):
                    _LOGGER.info("ipTIME 로그인 성공: %s UI (%s)", mode, self._base_url)
                    break
            except IptimeCaptchaRequired:
                raise
            except IptimeAuthenticationError as err:
                auth_error = err
                _LOGGER.debug("ipTIME %s 로그인 인증 실패: %s", mode, err)
            except UpdateFailed as err:
                last_error = err
                _LOGGER.debug("ipTIME %s 로그인 방식 실패: %s", mode, err)
        else:
            if auth_error:
                raise auth_error
            if last_error:
                raise last_error
            raise UpdateFailed("지원되는 ipTIME 로그인 방식을 찾지 못했습니다")

        self._mesh_enabled = await self._detect_mesh_enabled()
        if self._mesh_enabled:
            _LOGGER.info("ipTIME EasyMesh 감지됨: 위성 기기도 함께 추적합니다")
        return True

    async def _try_login_candidate(self, mode: str, login_method: Any) -> bool:
        """Attempt one login mode, validating that its client-list API works too.

        UI modes we positively detected (beta/mobile) get a single same-mode
        retry before login() moves on to a completely different protocol.
        Without this, one transient hiccup on the router's real, working API
        would cascade into hammering it with 2-3 other login sequences that
        are guaranteed not to exist on that firmware — amplifying load on a
        router that's already struggling instead of just trying again.
        """
        attempts = 2 if mode in {"beta", "mobile"} else 1
        last_err: UpdateFailed | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await asyncio.sleep(1)
            self._api_mode = mode
            self._logged_in = False
            await self._clear_session_cookies()
            try:
                await login_method()
                # A login cookie alone is not enough: some firmware exposes an
                # endpoint but not the matching client-list API. Validate the
                # complete mode before selecting it.
                await self.get_wireless_clients()
                return True
            except (IptimeCaptchaRequired, IptimeAuthenticationError):
                raise
            except UpdateFailed as err:
                last_err = err
                _LOGGER.debug(
                    "ipTIME %s 로그인 시도 %d/%d 실패: %s", mode, attempt, attempts, err
                )
        if last_err:
            raise last_err
        return False

    async def _login_beta(self) -> bool:
        response, payload = await self._request_json(
            "session/login",
            {"id": self._username, "pw": self._password},
            retry_on_auth=False,
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

    async def _login_original(self) -> bool:
        """Log in through the timepro form used by older ipTIME firmware."""
        response, text = await self._request_text(
            "POST",
            self.LEGACY_DATA_PATH,
            data={
                "tmenu": "iframe",
                "smenu": "login_proc",
                "username": self._username,
                "passwd": self._password,
            },
            allow_redirects=True,
        )
        return await self._accept_legacy_login_response(response, text)

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
        return await self._accept_legacy_login_response(response, text)

    async def _accept_legacy_login_response(
        self, response: aiohttp.ClientResponse, text: str
    ) -> bool:
        """Validate and retain a session from any classic login endpoint."""
        session_cookie = response.cookies.get("efm_session_id")
        session = await self._get_session()
        stored_cookie = session.cookie_jar.filter_cookies(response.url).get(
            "efm_session_id"
        )
        session_in_body = re.search(r"\b([A-Za-z0-9]{16})\b", text)
        is_login_page = self._is_login_page(text)
        captcha_required = self._captcha_required(text)
        logged_in = bool(session_cookie or stored_cookie or session_in_body)

        # Temporary diagnostic: capture everything needed to pin down a login
        # failure in one shot instead of another deploy-and-check round trip.
        # Covers both "session marker missing" (cookie name/scope/body-format
        # differs on this firmware) and "wrong credentials misclassified as
        # a connection failure" (is_login_page()/_captcha_required() rely on
        # fixed patterns from known firmware and may not match this one).
        if not logged_in or is_login_page or captcha_required:
            credential_failure_hints = [
                phrase
                for phrase in (
                    "wrong", "invalid", "incorrect", "실패", "틀렸", "일치하지",
                    "잘못", "fail", "denied", "error",
                )
                if phrase in text.lower()
            ]
            session_keyword_hits = re.findall(
                r"(?:sess|token|sid|efm)[a-z_]*\s*[:=]\s*[\"']?([\w-]{6,64})",
                text,
                re.IGNORECASE,
            )
            _LOGGER.debug(
                "ipTIME 로그인 진단: status=%s final_url=%s\n"
                "  classifier 판정: is_login_page=%s captcha_required=%s marker_found=%s\n"
                "  본문 내 실패 의심 키워드=%s (ID/PW가 맞는데도 이게 뜨면 classifier 오탐지 의심)\n"
                "  이 응답 Set-Cookie=%s\n"
                "  리다이렉트 이력=%s\n"
                "  쿠키 저장소=%s\n"
                "  session/token/sid/efm 키워드 주변 후보값(최대 10개)=%s\n"
                "  본문 길이=%d, 본문 앞부분=%r",
                response.status,
                response.url,
                is_login_page,
                captcha_required,
                logged_in,
                credential_failure_hints,
                response.headers.getall("Set-Cookie", []),
                [
                    {
                        "status": hop.status,
                        "url": str(hop.url),
                        "set_cookie": hop.headers.getall("Set-Cookie", []),
                    }
                    for hop in response.history
                ],
                [
                    f"{morsel.key}={morsel.value} (domain={morsel['domain']!r}, path={morsel['path']!r})"
                    for morsel in session.cookie_jar
                ],
                session_keyword_hits[:10],
                len(text),
                text[:500],
            )

        if captcha_required:
            raise IptimeCaptchaRequired("ipTIME 관리자 CAPTCHA 인증이 활성화되어 있습니다")
        if is_login_page:
            raise IptimeAuthenticationError("ipTIME 관리자 계정이 올바르지 않습니다")
        self._logged_in = logged_in
        if self._logged_in and not session_cookie and not stored_cookie and session_in_body:
            session.cookie_jar.update_cookies(
                {"efm_session_id": session_in_body.group(1)}, response.url
            )
        if not self._logged_in:
            raise UpdateFailed("ipTIME 로그인 응답에서 세션 값을 찾지 못했습니다")
        return True

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
        if self._captcha_required(text):
            raise IptimeCaptchaRequired("ipTIME 관리자 CAPTCHA 인증이 활성화되어 있습니다")
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
        if self._api_mode == "original":
            return await self._get_original_clients()
        return await self._get_legacy_clients()

    async def _get_beta_clients(self) -> list[WirelessClient]:
        _, payload = await self._request_json("network/interface/lan/stations")
        result = payload.get("result")
        if result is None:
            error = payload.get("error") or {}
            if error.get("code") == -31998:
                self._logged_in = False
                raise IptimeSessionExpired("ipTIME 세션이 만료되었습니다")
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
        return await self._get_mobile_clients_once(retry_on_auth=True)

    async def _get_mobile_clients_once(
        self, *, retry_on_auth: bool
    ) -> list[WirelessClient]:
        clients: dict[str, WirelessClient] = {}
        successful_requests = 0
        session_expired = False
        for interface, path in self.MOBILE_WLAN_PATHS.items():
            try:
                _, text = await self._request_text("GET", path)
            except UpdateFailed as err:
                _LOGGER.debug("ipTIME(모바일) %s 조회 실패: %s", interface, err)
                continue
            try:
                payload = json.loads(text)
            except ValueError as err:
                if self._is_login_page(text) or "m_handler.cgi" in text:
                    session_expired = True
                    continue
                _LOGGER.debug("ipTIME(모바일) %s 응답 파싱 실패: %s", interface, err)
                continue
            if not isinstance(payload, dict):
                continue
            if self._mobile_payload_unauthorized(payload):
                session_expired = True
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
        if session_expired:
            self._logged_in = False
            if retry_on_auth:
                _LOGGER.info("ipTIME 모바일 세션 만료 감지, 재로그인 후 재조회")
                await self._login_mobile()
                return await self._get_mobile_clients_once(retry_on_auth=False)
            raise IptimeSessionExpired("ipTIME 모바일 세션이 만료되었습니다")
        if not successful_requests:
            raise UpdateFailed("ipTIME(모바일) 무선 접속자 페이지를 조회할 수 없습니다")
        return list(clients.values())

    @classmethod
    def _mobile_payload_unauthorized(cls, payload: dict[str, Any]) -> bool:
        text = json.dumps(payload, ensure_ascii=False).lower()
        return any(
            marker in text
            for marker in (
                "login_session",
                "session expired",
                "session_expired",
                "invalid session",
                "not logged",
            )
        )

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
                    raise IptimeSessionExpired("ipTIME 세션이 만료되었습니다")
                successful_requests += 1
                for client in self._parse_wireless(page, interface):
                    clients[client.mac] = client
            except UpdateFailed as err:
                if isinstance(err, IptimeSessionExpired):
                    raise
                _LOGGER.debug("ipTIME %s 조회 실패: %s", interface, err)

        if not successful_requests:
            raise UpdateFailed("ipTIME 무선 접속자 페이지를 조회할 수 없습니다")
        return list(clients.values())

    async def _get_original_clients(self) -> list[WirelessClient]:
        """Query the POST menus used by older timepro-based firmware."""
        clients: dict[str, WirelessClient] = {}
        successful_requests = 0
        for menu, interface in (
            ("wireless_association", "2.4GHz"),
            ("wireless_association_5g", "5GHz"),
            ("wireless_association_6g", "6GHz"),
            ("lan_pcinfo_status", "LAN/unknown"),
        ):
            try:
                _, page = await self._request_text(
                    "POST",
                    self.LEGACY_DATA_PATH,
                    data={"tmenu": "iframe", "smenu": menu},
                )
            except UpdateFailed as err:
                _LOGGER.debug("ipTIME 원본 메뉴 %s 조회 실패: %s", menu, err)
                continue
            if self._is_login_page(page):
                self._logged_in = False
                raise IptimeSessionExpired("ipTIME 세션이 만료되었습니다")
            successful_requests += 1
            for client in self._parse_wireless(page, interface):
                clients[client.mac] = client
        if not successful_requests:
            raise UpdateFailed("ipTIME 원본 접속자 페이지를 조회할 수 없습니다")
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
            ip = next((valid for cell in cells if (valid := self._valid_ip(cell))), "")
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
        for method, request_data in (
            (
                "GET",
                {"params": {"tmenu": "iframe", "smenu": "lan_pcinfo_status"}},
            ),
            (
                "POST",
                {"data": {"tmenu": "iframe", "smenu": "lan_pcinfo_status"}},
            ),
        ):
            try:
                _, page = await self._request_text(
                    method, self.LEGACY_DATA_PATH, **request_data
                )
            except UpdateFailed as err:
                _LOGGER.debug("ipTIME DHCP 조회(%s) 실패: %s", method, err)
                continue
            if self._is_login_page(page):
                self._logged_in = False
                raise IptimeSessionExpired("ipTIME DHCP 조회 중 세션이 만료되었습니다")
            if leases := self._parse_dhcp_leases(page):
                return leases
        return []

    def _parse_dhcp_leases(self, page: str) -> list[DhcpLease]:
        leases: list[DhcpLease] = []
        for row in self._ROW_PATTERN.finditer(page):
            row_text = row.group()
            mac_match = self._MAC_PATTERN.search(row_text)
            ip = self._valid_ip(row_text)
            if not mac_match or not ip:
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
            leases.append(DhcpLease(mac=mac, ip=ip, hostname=hostname))
        return leases

    async def get_static_leases(self) -> list[StaticLease]:
        # Menu names differ between firmware generations. An unsupported menu
        # is harmless; only return once a page contains parseable mappings.
        for menu in ("lan_dhcp_static", "lan_dhcp_staticlist", "lan_dhcp_manual"):
            try:
                _, page = await self._request_text(
                    "GET",
                    self.LEGACY_DATA_PATH,
                    params={"tmenu": "iframe", "smenu": menu},
                )
            except UpdateFailed:
                continue
            if self._is_login_page(page):
                self._logged_in = False
                raise IptimeSessionExpired(
                    "ipTIME 고정 DHCP 조회 중 세션이 만료되었습니다"
                )
            if "static" not in page.lower() and "고정" not in page:
                continue
            leases = [
                StaticLease(mac=item.mac, ip=item.ip, hostname=item.hostname)
                for item in self._parse_dhcp_leases(page)
            ]
            if leases:
                return leases
        return []

    async def fetch_all(self, rssi_limit: int = DEFAULT_RSSI_LIMIT) -> IptimeData:
        if not self._logged_in and not await self.login():
            raise UpdateFailed("ipTIME 로그인 실패: 계정 또는 CAPTCHA 설정을 확인하세요")

        try:
            wireless = await self.get_wireless_clients()
        except IptimeSessionExpired:
            _LOGGER.info("ipTIME 세션 만료 감지, 재로그인 후 재조회합니다")
            await self.login()  # raises with the specific reason if this fails too
            wireless = await self.get_wireless_clients()

        # timepro's client page is also available in many mobile/classic builds
        # and fills in wired devices that the mobile WLAN API does not expose.
        dhcp = []
        static = []
        if self._api_mode in {"mobile", "legacy", "original"}:
            try:
                dhcp = await self.get_dhcp_leases()
                static = await self.get_static_leases()
            except IptimeSessionExpired:
                _LOGGER.info("ipTIME 보조 목록 조회 중 세션 만료, 재로그인 후 재조회")
                await self.login()
                dhcp = await self.get_dhcp_leases()
                static = await self.get_static_leases()
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
            static_leases=static,
        )

    @classmethod
    def _clean_cell(cls, value: str) -> str:
        return " ".join(html_lib.unescape(cls._TAG_PATTERN.sub("", value)).split())

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

    @staticmethod
    def _is_login_page(page: str) -> bool:
        page = page.lower()
        return (
            'name="passwd"' in page
            or "login_session.cgi?noauto=1" in page
            or "efm_pwchk" in page
        )

    @staticmethod
    def _captcha_required(page: str) -> bool:
        lowered = page.lower()
        if re.search(
            r"captcha(?:_on)?\s*[:=]\s*[\"']?(?:0|false|off)\b", lowered
        ):
            return False
        return bool(
            re.search(r'name=[\"\'](?:captcha|captcha_code)[\"\']', lowered)
            or "captcha_required" in lowered
        )


class IptimeDataUpdateCoordinator(DataUpdateCoordinator[IptimeData]):
    """Coordinate periodic ipTIME client updates."""

    def __init__(
        self, hass: HomeAssistant, client: IptimeClient, entry: ConfigEntry
    ) -> None:
        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> IptimeData:
        rssi_limit = self.entry.options.get(
            CONF_RSSI_LIMIT, self.entry.data.get(CONF_RSSI_LIMIT, DEFAULT_RSSI_LIMIT)
        )
        return await self.client.fetch_all(rssi_limit=rssi_limit)
