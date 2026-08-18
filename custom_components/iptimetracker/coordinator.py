from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class WirelessClient:
    mac: str
    ip: str
    hostname: str
    interface: str  # 2.4GHz / 5GHz / 6GHz
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
    wireless_clients: list[WirelessClient] = field(default_factory=list)
    dhcp_leases: list[DhcpLease] = field(default_factory=list)
    static_leases: list[StaticLease] = field(default_factory=list)


class IptimeClient:
    """ipTIME 공유기 HTTP API 클라이언트."""

    LOGIN_PATH = "/sess-bin/timepro.cgi"

    def __init__(self, host: str, username: str, password: str) -> None:
        self._base_url = f"http://{host}"
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> bool:
        session = await self._get_session()
        data = {
            "tmenu": "iframe",
            "smenu": "login_proc",
            "username": self._username,
            "passwd": self._password,
        }
        try:
            async with session.post(
                f"{self._base_url}{self.LOGIN_PATH}",
                data=data,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as resp:
                text = await resp.text()
                # 로그인 폼 입력 필드가 응답에 남아있으면 실패
                login_failed = (
                    'name="passwd"' in text
                    or 'name="username"' in text
                    or "efm_pwchk" in text
                )
                self._logged_in = not login_failed
                return self._logged_in
        except aiohttp.ClientError as err:
            self._logged_in = False
            raise UpdateFailed(f"공유기 연결 실패: {err}") from err

    async def _fetch(self, smenu: str) -> str:
        """ipTIME은 데이터 조회도 POST 방식 사용."""
        session = await self._get_session()
        data = {"tmenu": "iframe", "smenu": smenu}
        try:
            async with session.post(
                f"{self._base_url}{self.LOGIN_PATH}",
                data=data,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                # 세션 만료로 로그인 페이지가 반환된 경우
                if 'name="passwd"' in text or "efm_pwchk" in text:
                    self._logged_in = False
                    raise UpdateFailed(f"세션 만료 ({smenu}), 재로그인 필요")
                return text
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"데이터 조회 실패 ({smenu}): {err}") from err

    async def get_wireless_clients(self) -> list[WirelessClient]:
        clients: list[WirelessClient] = []

        for smenu, interface in [
            ("wireless_association", "2.4GHz"),
            ("wireless_association_5g", "5GHz"),
            ("wireless_association_6g", "6GHz"),
        ]:
            try:
                html = await self._fetch(smenu)
                clients.extend(self._parse_wireless(html, interface))
            except UpdateFailed:
                pass

        return clients

    def _parse_wireless(self, html: str, interface: str) -> list[WirelessClient]:
        clients: list[WirelessClient] = []
        # ipTIME HTML 테이블에서 MAC / IP / hostname / RSSI 파싱
        # 열 패턴: MAC | IP | hostname | RSSI | ...
        row_pattern = re.compile(
            r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE
        )
        mac_pattern = re.compile(
            r"([0-9A-Fa-f]{2}(?:[:\-][0-9A-Fa-f]{2}){5})"
        )
        ip_pattern = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
        rssi_pattern = re.compile(r"(-\d+)\s*dBm", re.IGNORECASE)
        cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
        tag_pattern = re.compile(r"<[^>]+>")

        for row in row_pattern.finditer(html):
            row_text = row.group()
            mac_match = mac_pattern.search(row_text)
            if not mac_match:
                continue

            mac = mac_match.group(1).upper().replace("-", ":")
            cells = [
                tag_pattern.sub("", c.group(1)).strip()
                for c in cell_pattern.finditer(row_text)
            ]

            ip = ""
            hostname = ""
            rssi = None

            for cell in cells:
                if ip_pattern.fullmatch(cell.strip()):
                    ip = cell.strip()
                rssi_m = rssi_pattern.search(cell)
                if rssi_m:
                    rssi = int(rssi_m.group(1))
                if cell and not mac_pattern.search(cell) and not ip_pattern.fullmatch(cell.strip()) and not rssi_pattern.search(cell):
                    if len(cell) > 1:
                        hostname = cell

            clients.append(
                WirelessClient(
                    mac=mac,
                    ip=ip,
                    hostname=hostname or mac,
                    interface=interface,
                    rssi=rssi,
                )
            )

        return clients

    async def get_dhcp_leases(self) -> list[DhcpLease]:
        html = await self._fetch("expertconfview_dhcp_status")
        return self._parse_dhcp_leases(html)

    def _parse_dhcp_leases(self, html: str) -> list[DhcpLease]:
        leases: list[DhcpLease] = []
        row_pattern = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
        mac_pattern = re.compile(r"([0-9A-Fa-f]{2}(?:[:\-][0-9A-Fa-f]{2}){5})")
        ip_pattern = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
        cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
        tag_pattern = re.compile(r"<[^>]+>")

        for row in row_pattern.finditer(html):
            row_text = row.group()
            mac_match = mac_pattern.search(row_text)
            if not mac_match:
                continue

            mac = mac_match.group(1).upper().replace("-", ":")
            cells = [
                tag_pattern.sub("", c.group(1)).strip()
                for c in cell_pattern.finditer(row_text)
            ]

            ip = ""
            hostname = ""
            expires = None

            for cell in cells:
                if ip_pattern.fullmatch(cell.strip()) and not ip:
                    ip = cell.strip()
                elif not mac_pattern.search(cell) and not ip_pattern.fullmatch(cell.strip()) and len(cell) > 1:
                    if "/" in cell or ":" in cell:
                        expires = cell
                    elif not hostname:
                        hostname = cell

            leases.append(
                DhcpLease(mac=mac, ip=ip, hostname=hostname or mac, expires=expires)
            )

        return leases

    async def get_static_leases(self) -> list[StaticLease]:
        html = await self._fetch("expertconfview_dhcp")
        return self._parse_static_leases(html)

    def _parse_static_leases(self, html: str) -> list[StaticLease]:
        leases: list[StaticLease] = []
        row_pattern = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
        mac_pattern = re.compile(r"([0-9A-Fa-f]{2}(?:[:\-][0-9A-Fa-f]{2}){5})")
        ip_pattern = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
        cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
        tag_pattern = re.compile(r"<[^>]+>")

        for row in row_pattern.finditer(html):
            row_text = row.group()
            mac_match = mac_pattern.search(row_text)
            if not mac_match:
                continue

            mac = mac_match.group(1).upper().replace("-", ":")
            cells = [
                tag_pattern.sub("", c.group(1)).strip()
                for c in cell_pattern.finditer(row_text)
            ]

            ip = ""
            hostname = ""

            for cell in cells:
                if ip_pattern.fullmatch(cell.strip()) and not ip:
                    ip = cell.strip()
                elif not mac_pattern.search(cell) and not ip_pattern.fullmatch(cell.strip()) and len(cell) > 1 and not hostname:
                    hostname = cell

            if ip:
                leases.append(StaticLease(mac=mac, ip=ip, hostname=hostname or mac))

        return leases

    async def fetch_all(self) -> IptimeData:
        # 세션이 없거나 만료된 경우에만 재로그인
        if not self._logged_in:
            logged_in = await self.login()
            if not logged_in:
                raise UpdateFailed("로그인 실패: ID 또는 비밀번호를 확인하세요.")

        try:
            wireless = await self.get_wireless_clients()
            dhcp = await self.get_dhcp_leases()
            static = await self.get_static_leases()
        except UpdateFailed as err:
            if "세션 만료" in str(err):
                # 재로그인 후 1회 재시도
                logged_in = await self.login()
                if not logged_in:
                    raise UpdateFailed("재로그인 실패") from err
                wireless = await self.get_wireless_clients()
                dhcp = await self.get_dhcp_leases()
                static = await self.get_static_leases()
            else:
                raise

        return IptimeData(
            wireless_clients=wireless,
            dhcp_leases=dhcp,
            static_leases=static,
        )


class IptimeDataUpdateCoordinator(DataUpdateCoordinator[IptimeData]):
    """ipTIME 데이터 업데이트 코디네이터."""

    def __init__(self, hass: HomeAssistant, client: IptimeClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> IptimeData:
        return await self.client.fetch_all()
