"""Standalone ipTIME login and connected-device diagnostic app.

This tool deliberately has no Home Assistant or third-party dependencies.
Run it with ``python tools/iptime_probe.py`` for the GUI or add ``--cli`` for
terminal use.
"""

from __future__ import annotations

import argparse
import getpass
import html
import http.cookiejar
import json
import queue
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class ProbeError(Exception):
    """Base error shown to the user."""


class AuthenticationError(ProbeError):
    """The router rejected the supplied credentials."""


class CaptchaRequiredError(ProbeError):
    """The router requires interactive CAPTCHA authentication."""


@dataclass
class ConnectedDevice:
    mac: str
    ip: str = ""
    hostname: str = ""
    connection: str = "unknown"
    rssi: int | None = None


class IptimeProbe:
    """Synchronous client intended for quick interactive protocol testing."""

    _MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})")
    _IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
    _RSSI_RE = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)
    _ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
    _CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
    _TAG_RE = re.compile(r"<[^>]+>")

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        log: Callable[[str], None] = print,
        timeout: float = 10,
    ) -> None:
        host = host.strip().rstrip("/")
        if not urllib.parse.urlsplit(host).scheme:
            host = f"http://{host}"
        self.base_url = host
        self.username = username
        self.password = password
        self.log = log
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.mode: str | None = None

    def run(self) -> list[ConnectedDevice]:
        self.log(f"공유기 접속 확인: {self.base_url}")
        beta = self._supports_beta_ui()
        self.mode = "beta" if beta else "legacy"
        self.log(f"관리 UI 감지: {'신형(Beta)' if beta else '구형(Classic)'}")

        if beta:
            self._login_beta()
            devices = self._fetch_beta_devices()
        else:
            self._login_legacy()
            devices = self._fetch_legacy_devices()

        self.log(f"조회 완료: 현재 접속 기기 {len(devices)}대")
        return devices

    def _request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        data: bytes | None = None
        headers = {
            "User-Agent": "Mozilla/5.0 (ipTIME Probe)",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                text = body.decode(charset, errors="replace")
                content_type = response.headers.get_content_type()
                self.log(
                    f"HTTP {response.status} {method} {path.split('?')[0]} "
                    f"({content_type}, {len(body)} bytes)"
                )
                return text, response.geturl()
        except urllib.error.HTTPError as error:
            raise ProbeError(f"HTTP {error.code}: {path.split('?')[0]}") from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            raise ProbeError(f"연결 실패: {reason}") from error
        except TimeoutError as error:
            raise ProbeError("연결 시간 초과") from error

    def _request_json(
        self, method_name: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method_name}
        if params is not None:
            payload["params"] = params
        text, _ = self._request("POST", "/cgi/service.cgi", payload=payload)
        try:
            result = json.loads(text)
        except ValueError as error:
            raise ProbeError(
                f"{method_name}: JSON이 아닌 응답을 받았습니다 ({len(text)}자)"
            ) from error
        if not isinstance(result, dict):
            raise ProbeError(f"{method_name}: 예상하지 못한 JSON 형식입니다")
        return result

    def _supports_beta_ui(self) -> bool:
        for path in ("/ui/flutter_bootstrap.js", "/ui/"):
            try:
                text, _ = self._request("GET", path)
            except ProbeError as error:
                self.log(f"신형 UI 확인 실패({path}): {error}")
                continue
            if "/cgi/service.cgi" in text or "flutter" in text.lower():
                return True
        return False

    def _login_beta(self) -> None:
        response = self._request_json(
            "session/login", {"id": self.username, "pw": self.password}
        )
        if response.get("result"):
            self.log("신형 UI 로그인 성공")
            return
        error = response.get("error") or {}
        code = error.get("code")
        if code == -31997:
            raise CaptchaRequiredError(
                "문자입력(CAPTCHA) 인증이 활성화되어 자동 로그인할 수 없습니다"
            )
        if code == -31996:
            raise AuthenticationError("관리자 아이디 또는 비밀번호가 틀렸습니다")
        raise ProbeError(f"신형 UI 로그인 실패: error code {code}")

    def _login_legacy(self) -> None:
        text, response_url = self._request(
            "POST",
            "/sess-bin/login_handler.cgi",
            form={"username": self.username, "passwd": self.password},
        )
        lower = text.lower()
        if "captcha" in lower and "captcha_on" not in lower:
            raise CaptchaRequiredError(
                "문자입력(CAPTCHA) 인증이 활성화되어 자동 로그인할 수 없습니다"
            )
        if (
            "login_session.cgi?noauto=1" in text
            or 'name="passwd"' in text
            or "efm_pwchk" in text
        ):
            raise AuthenticationError("관리자 아이디 또는 비밀번호가 틀렸습니다")

        if not self._has_session_cookie():
            match = re.search(r"\b([A-Za-z0-9]{16})\b", text)
            if match:
                parsed = urllib.parse.urlsplit(response_url)
                self.cookies.set_cookie(
                    http.cookiejar.Cookie(
                        version=0,
                        name="efm_session_id",
                        value=match.group(1),
                        port=None,
                        port_specified=False,
                        domain=parsed.hostname or "",
                        domain_specified=False,
                        domain_initial_dot=False,
                        path="/",
                        path_specified=True,
                        secure=parsed.scheme == "https",
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                )
        if not self._has_session_cookie():
            raise ProbeError("로그인 응답에서 세션 값을 찾지 못했습니다")
        self.log("구형 UI 로그인 성공")

    def _has_session_cookie(self) -> bool:
        return any(cookie.name == "efm_session_id" for cookie in self.cookies)

    def _fetch_beta_devices(self) -> list[ConnectedDevice]:
        response = self._request_json("network/interface/lan/stations")
        result = response.get("result")
        if result is None:
            error = response.get("error") or {}
            if error.get("code") == -31998:
                raise AuthenticationError("조회 전에 세션이 만료되었습니다")
            raise ProbeError(f"접속자 조회 실패: error code {error.get('code')}")

        devices: list[ConnectedDevice] = []
        for item in result:
            connection = item.get("connection") or {}
            connection_type = str(connection.get("type") or "unknown")
            details = connection.get(connection_type) or {}
            info = item.get("info") or {}
            bss = str(details.get("bss") or connection_type)
            label = {"2g.1": "2.4GHz", "5g.1": "5GHz", "6g.1": "6GHz"}.get(
                bss, bss
            )
            devices.append(
                ConnectedDevice(
                    mac=self._normalize_mac(str(item.get("mac") or "")),
                    ip=str(info.get("ip") or ""),
                    hostname=str(info.get("name") or info.get("hostname") or ""),
                    connection=label,
                    rssi=self._as_int(details.get("rssi")),
                )
            )
        return [device for device in devices if device.mac]

    def _fetch_legacy_devices(self) -> list[ConnectedDevice]:
        devices: dict[str, ConnectedDevice] = {}
        for bssidx, label in (("0", "2.4GHz"), ("65536", "5GHz")):
            query = urllib.parse.urlencode(
                {
                    "tmenu": "iframe",
                    "smenu": "macauth_pcinfo_status",
                    "bssidx": bssidx,
                }
            )
            text, _ = self._request("GET", f"/sess-bin/timepro.cgi?{query}")
            self._ensure_not_login_page(text)
            for device in self.parse_html_devices(text, label):
                devices[device.mac] = device

        query = urllib.parse.urlencode(
            {"tmenu": "iframe", "smenu": "lan_pcinfo_status"}
        )
        text, _ = self._request("GET", f"/sess-bin/timepro.cgi?{query}")
        self._ensure_not_login_page(text)
        for device in self.parse_html_devices(text, "LAN/unknown"):
            devices.setdefault(device.mac, device)
        return list(devices.values())

    @classmethod
    def parse_html_devices(
        cls, document: str, connection: str
    ) -> list[ConnectedDevice]:
        devices: list[ConnectedDevice] = []
        for row in cls._ROW_RE.finditer(document):
            row_text = row.group()
            mac_match = cls._MAC_RE.search(row_text)
            if not mac_match:
                continue
            cells = [
                " ".join(html.unescape(cls._TAG_RE.sub("", cell.group(1))).split())
                for cell in cls._CELL_RE.finditer(row_text)
            ]
            ip_match = cls._IP_RE.search(" ".join(cells))
            rssi_match = cls._RSSI_RE.search(" ".join(cells))
            mac = cls._normalize_mac(mac_match.group(1))
            hostname = next(
                (
                    cell
                    for cell in cells
                    if cell
                    and not cls._MAC_RE.search(cell)
                    and not cls._IP_RE.search(cell)
                    and not cls._RSSI_RE.search(cell)
                ),
                "",
            )
            devices.append(
                ConnectedDevice(
                    mac=mac,
                    ip=ip_match.group(1) if ip_match else "",
                    hostname=hostname,
                    connection=connection,
                    rssi=int(rssi_match.group(1)) if rssi_match else None,
                )
            )
        return devices

    @classmethod
    def _normalize_mac(cls, value: str) -> str:
        match = cls._MAC_RE.search(value)
        return match.group(1).upper().replace("-", ":") if match else ""

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ensure_not_login_page(text: str) -> None:
        if (
            'name="passwd"' in text
            or "login_session.cgi?noauto=1" in text
            or "efm_pwchk" in text
        ):
            raise AuthenticationError("세션이 없거나 만료되었습니다")


def format_devices(devices: list[ConnectedDevice]) -> list[str]:
    if not devices:
        return ["현재 접속 중인 기기가 없습니다."]
    rows = ["MAC                IP              연결       RSSI   이름"]
    rows.append("-" * 72)
    for device in devices:
        rssi = str(device.rssi) if device.rssi is not None else "-"
        rows.append(
            f"{device.mac:<18} {device.ip:<15} {device.connection:<10} "
            f"{rssi:<6} {device.hostname}"
        )
    return rows


def run_cli(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("관리자 비밀번호: ")
    probe = IptimeProbe(args.host, args.username, password)
    try:
        devices = probe.run()
    except ProbeError as error:
        print(f"실패: {error}")
        return 1
    for row in format_devices(devices):
        print(row)
    return 0


def run_gui(default_host: str, default_username: str) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("ipTIME 접속자 테스트")
    root.geometry("820x560")
    root.minsize(700, 420)

    form = ttk.Frame(root, padding=12)
    form.pack(fill="x")
    output = tk.Text(root, wrap="none", font=("Consolas", 10), state="disabled")
    output.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    host_var = tk.StringVar(value=default_host)
    username_var = tk.StringVar(value=default_username)
    password_var = tk.StringVar()
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

    ttk.Label(form, text="공유기 주소").grid(row=0, column=0, sticky="w")
    ttk.Entry(form, textvariable=host_var, width=28).grid(
        row=1, column=0, padx=(0, 8), sticky="ew"
    )
    ttk.Label(form, text="관리자 아이디").grid(row=0, column=1, sticky="w")
    ttk.Entry(form, textvariable=username_var, width=18).grid(
        row=1, column=1, padx=(0, 8), sticky="ew"
    )
    ttk.Label(form, text="관리자 비밀번호").grid(row=0, column=2, sticky="w")
    ttk.Entry(form, textvariable=password_var, show="*", width=22).grid(
        row=1, column=2, padx=(0, 8), sticky="ew"
    )
    form.columnconfigure(0, weight=1)
    form.columnconfigure(2, weight=1)

    def append(message: str) -> None:
        output.configure(state="normal")
        output.insert("end", message + "\n")
        output.see("end")
        output.configure(state="disabled")

    def worker(host: str, username: str, password: str) -> None:
        probe = IptimeProbe(
            host,
            username,
            password,
            log=lambda message: result_queue.put(("log", message)),
        )
        try:
            result_queue.put(("devices", probe.run()))
        except Exception as error:  # noqa: BLE001 - present errors in the GUI
            result_queue.put(("error", error))

    def start() -> None:
        if str(test_button["state"]) == "disabled":
            return
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.configure(state="disabled")
        test_button.configure(state="disabled")
        values = (host_var.get(), username_var.get(), password_var.get())
        threading.Thread(target=worker, args=values, daemon=True).start()

    def poll() -> None:
        try:
            while True:
                kind, value = result_queue.get_nowait()
                if kind == "log":
                    append(str(value))
                elif kind == "devices":
                    append("")
                    for row in format_devices(value):
                        append(row)
                    test_button.configure(state="normal")
                else:
                    append(f"\n실패: {value}")
                    test_button.configure(state="normal")
        except queue.Empty:
            pass
        root.after(100, poll)

    test_button = ttk.Button(form, text="로그인 및 접속자 조회", command=start)
    test_button.grid(row=1, column=3)
    root.bind("<Return>", lambda _event: start())
    root.after(100, poll)
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone ipTIME connection tester")
    parser.add_argument("--cli", action="store_true", help="터미널 모드로 실행")
    parser.add_argument("--host", default="192.168.0.1", help="공유기 주소")
    parser.add_argument("--username", default="admin", help="관리자 아이디")
    parser.add_argument("--password", help="관리자 비밀번호(생략하면 안전하게 입력)")
    args = parser.parse_args()
    if args.cli:
        return run_cli(args)
    run_gui(args.host, args.username)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
