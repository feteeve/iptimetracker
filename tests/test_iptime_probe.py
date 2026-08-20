from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "iptime_probe.py"
SPEC = importlib.util.spec_from_file_location("iptime_probe", MODULE_PATH)
assert SPEC and SPEC.loader
iptime_probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = iptime_probe
SPEC.loader.exec_module(iptime_probe)


class IptimeProbeParserTest(unittest.TestCase):
    def test_parses_legacy_html_row(self) -> None:
        document = """
        <table><tr>
          <td>AA-BB-CC-DD-EE-FF</td>
          <td>192.168.0.20</td>
          <td>living-room-tv</td>
          <td>-51 dBm</td>
        </tr></table>
        """
        devices = iptime_probe.IptimeProbe.parse_html_devices(document, "5GHz")

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(devices[0].ip, "192.168.0.20")
        self.assertEqual(devices[0].hostname, "living-room-tv")
        self.assertEqual(devices[0].rssi, -51)

    def test_normalizes_host(self) -> None:
        probe = iptime_probe.IptimeProbe("192.168.0.1/", "admin", "secret")
        self.assertEqual(probe.base_url, "http://192.168.0.1")

    def test_formats_empty_device_list(self) -> None:
        self.assertEqual(
            iptime_probe.format_devices([]), ["현재 접속 중인 기기가 없습니다."]
        )


class _BetaRouterHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ui/flutter_bootstrap.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(b"const service = '/cgi/service.cgi';")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if payload["method"] == "session/login":
            result = {"result": True}
            self.send_response(200)
            self.send_header("Set-Cookie", "efm_session_id=0123456789ABCDEF; Path=/")
        elif payload["method"] == "network/interface/lan/stations":
            if "efm_session_id=0123456789ABCDEF" not in self.headers.get("Cookie", ""):
                result = {"error": {"code": -31998}}
            else:
                result = {
                    "result": [
                        {
                            "mac": "AA:BB:CC:DD:EE:FF",
                            "info": {"ip": "192.168.0.20", "name": "phone"},
                            "connection": {
                                "type": "wireless",
                                "wireless": {"bss": "5g.1", "rssi": -48},
                            },
                        },
                        {
                            "mac": "11:22:33:44:55:66",
                            "info": {"ip": "192.168.0.30", "name": "desktop"},
                            "connection": {"type": "wired", "wired": {}},
                        },
                    ]
                }
            self.send_response(200)
        else:
            result = {"error": {"code": -1}}
            self.send_response(400)
        body = json.dumps(result).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IptimeProbeNetworkTest(unittest.TestCase):
    def test_beta_login_cookie_and_all_devices(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BetaRouterHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        probe = iptime_probe.IptimeProbe(
            f"http://127.0.0.1:{server.server_port}",
            "admin",
            "secret",
            log=lambda _message: None,
        )
        devices = probe.run()

        self.assertEqual(probe.mode, "beta")
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].connection, "5GHz")
        self.assertEqual(devices[1].connection, "wired")


class _OriginalRouterHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        menu = form.get("smenu", [""])[0]
        self.send_response(200)
        if menu == "login_proc":
            self.send_header("Set-Cookie", "efm_session_id=FEDCBA9876543210; Path=/")
            body = b"<html>login complete</html>"
        elif "efm_session_id=FEDCBA9876543210" not in self.headers.get("Cookie", ""):
            body = b'<input name="passwd">'
        elif menu == "lan_pcinfo_status":
            body = (
                b"<tr><td>11-22-33-44-55-66</td>"
                b"<td>192.168.0.30</td><td>desktop</td></tr>"
            )
        else:
            body = b"<table></table>"
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IptimeProbeOriginalNetworkTest(unittest.TestCase):
    def test_original_login_and_post_menus(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OriginalRouterHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        probe = iptime_probe.IptimeProbe(
            f"http://127.0.0.1:{server.server_port}",
            "admin",
            "secret",
            log=lambda _message: None,
            mode="original",
        )
        devices = probe.run()

        self.assertEqual(probe.mode, "original")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].hostname, "desktop")


if __name__ == "__main__":
    unittest.main()
