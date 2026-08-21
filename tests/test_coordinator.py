from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).parents[1]


def _install_import_stubs() -> None:
    """Provide the small HA/aiohttp surface needed by these client unit tests."""
    package = ModuleType("custom_components.iptimetracker")
    package.__path__ = [str(ROOT / "custom_components" / "iptimetracker")]
    sys.modules.setdefault("custom_components", ModuleType("custom_components"))
    sys.modules[package.__name__] = package

    aiohttp = ModuleType("aiohttp")
    aiohttp.ClientSession = object
    aiohttp.ClientResponse = object
    aiohttp.ClientError = OSError
    aiohttp.CookieJar = object
    aiohttp.ClientTimeout = object
    sys.modules["aiohttp"] = aiohttp

    homeassistant = ModuleType("homeassistant")
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = ModuleType("homeassistant.helpers")
    coordinator = ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    coordinator.UpdateFailed = UpdateFailed
    coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = coordinator


_install_import_stubs()
MODULE_PATH = ROOT / "custom_components" / "iptimetracker" / "coordinator.py"
SPEC = importlib.util.spec_from_file_location(
    "custom_components.iptimetracker.coordinator", MODULE_PATH
)
assert SPEC and SPEC.loader
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)


class IptimeClientHelperTest(unittest.TestCase):
    def test_normalizes_router_origin(self) -> None:
        self.assertEqual(
            coordinator.IptimeClient.normalize_host("192.168.0.1/admin/"),
            "http://192.168.0.1",
        )
        self.assertEqual(
            coordinator.IptimeClient.normalize_host("HTTPS://Router.Local:8443/ui"),
            "https://router.local:8443",
        )

    def test_rejects_invalid_ipv4_octets_in_html(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        page = """
        <tr><td>AA-BB-CC-DD-EE-FF</td><td>999.1.1.1</td>
        <td>phone</td><td>-50 dBm</td></tr>
        """
        parsed = client._parse_wireless(page, "5GHz")
        self.assertEqual(parsed[0].ip, "")
        self.assertEqual(parsed[0].mac, "AA:BB:CC:DD:EE:FF")

    def test_classic_lan_layout_uses_hostname_not_assignment_column(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        page = """
        <tr class="lansetup_main_tr">
          <td><span>192.168.0.12</span></td>
          <td><span>AA:BB:CC:DD:EE:FF</span></td>
          <td><span>my-phone</span></td>
          <td><span>무선 : 수동할당</span></td>
        </tr>
        """
        parsed = client._parse_wireless(page, "LAN/unknown")
        self.assertEqual(parsed[0].hostname, "my-phone")

    def test_address_parser_uses_reordered_headers(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        page = """
        <table>
          <tr><th>장치 이름</th><th>MAC 주소</th><th>할당 방식</th><th>IP 주소</th></tr>
          <tr><td>living-room-tv</td><td>AA-BB-CC-DD-EE-FF</td>
              <td>Static</td><td>192.168.0.30</td></tr>
        </table>
        """
        self.assertEqual(
            client._parse_address_rows(page),
            [
                (
                    "AA:BB:CC:DD:EE:FF",
                    "192.168.0.30",
                    "living-room-tv",
                    "Static",
                )
            ],
        )

    def test_assignment_text_is_not_accepted_as_a_name(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        self.assertEqual(client._clean_name("무선 : 수동할당", "AA:BB:CC:DD:EE:FF"), "")

    def test_name_alias_fields_are_supported(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        self.assertEqual(
            client._name_from_mappings(
                "AA:BB:CC:DD:EE:FF", {"nickname": "거실 스피커"}
            ),
            "거실 스피커",
        )

    def test_mobile_unauthorized_payload_detection(self) -> None:
        self.assertTrue(
            coordinator.IptimeClient._mobile_payload_unauthorized(
                {"error": "session expired"}
            )
        )
        self.assertFalse(
            coordinator.IptimeClient._mobile_payload_unauthorized({"stalist": []})
        )

    def test_captcha_detection_ignores_disabled_flag(self) -> None:
        self.assertFalse(
            coordinator.IptimeClient._captcha_required("captcha_on = 0")
        )
        self.assertTrue(
            coordinator.IptimeClient._captcha_required(
                '<input name="captcha_code" type="text">'
            )
        )


class IptimeClientAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_beta_login_never_reauthenticates_itself(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._username = "admin"
        client._password = "wrong"
        client._request_json = AsyncMock(
            return_value=(
                SimpleNamespace(url="http://router/", cookies={}),
                {"error": {"code": -31996, "message": "login failed"}},
            )
        )
        with self.assertRaises(coordinator.IptimeAuthenticationError):
            await client._login_beta()
        self.assertFalse(client._request_json.call_args.kwargs["retry_on_auth"])

    async def test_failed_detected_mode_falls_back_to_original(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._base_url = "http://router"
        client._logged_in = False
        client._mesh_enabled = False
        client._api_mode = None
        client._supports_beta_ui = AsyncMock(return_value=True)
        client._supports_mobile_ui = AsyncMock(return_value=False)
        client._clear_session_cookies = AsyncMock()
        client._login_beta = AsyncMock(return_value=True)
        client._login_original = AsyncMock(return_value=True)
        client._login_legacy = AsyncMock(return_value=True)
        client.get_wireless_clients = AsyncMock(
            side_effect=[
                coordinator.UpdateFailed("unsupported"),
                coordinator.UpdateFailed("still unsupported"),
                [],
            ]
        )
        client._detect_mesh_enabled = AsyncMock(return_value=False)

        self.assertTrue(await client.login())
        self.assertEqual(client._api_mode, "original")
        client._login_original.assert_awaited_once()

    async def test_mobile_session_expiry_relogs_once(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._logged_in = True
        login_page = '<html><input name="passwd"></html>'
        empty = json.dumps({"stalist": []})
        client._request_text = AsyncMock(
            side_effect=[
                (None, login_page),
                (None, login_page),
                (None, empty),
                (None, empty),
            ]
        )
        client._login_mobile = AsyncMock(return_value=True)

        self.assertEqual(await client._get_mobile_clients(), [])
        client._login_mobile.assert_awaited_once()

    async def test_static_page_parses_offline_named_reservation(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._api_mode = "legacy"
        client._logged_in = True
        page = """
        <h2>등록된 주소 관리</h2>
        <table>
          <tr><th>IP 주소</th><th>MAC 주소</th><th>기기 이름</th></tr>
          <tr><td>192.168.0.40</td><td>AA:BB:CC:DD:EE:FF</td><td>아빠폰</td></tr>
        </table>
        """
        client._request_text = AsyncMock(return_value=(None, page))

        leases = await client.get_static_leases()

        self.assertEqual(
            leases,
            [
                coordinator.StaticLease(
                    mac="AA:BB:CC:DD:EE:FF",
                    ip="192.168.0.40",
                    hostname="아빠폰",
                )
            ],
        )
        self.assertEqual(client._static_route, ("GET", "lan_dhcp"))
        self.assertEqual(await client.get_static_leases(), leases)
        self.assertEqual(client._request_text.await_count, 2)

    async def test_fetch_all_enriches_names_on_beta_without_overwriting_device_name(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._logged_in = True
        client._api_mode = "beta"
        client._mesh_enabled = False
        client.get_wireless_clients = AsyncMock(
            return_value=[
                coordinator.WirelessClient(
                    mac="AA:BB:CC:DD:EE:FF",
                    ip="192.168.0.41",
                    hostname="Galaxy-S24",
                    interface="5GHz",
                )
            ]
        )
        client.get_dhcp_leases = AsyncMock(
            return_value=[
                coordinator.DhcpLease(
                    mac="AA:BB:CC:DD:EE:FF",
                    ip="192.168.0.41",
                    hostname="dhcp-phone",
                )
            ]
        )
        client.get_static_leases = AsyncMock(
            return_value=[
                coordinator.StaticLease(
                    mac="AA:BB:CC:DD:EE:FF",
                    ip="192.168.0.41",
                    hostname="아빠폰",
                )
            ]
        )
        client.get_wan_link_status = AsyncMock(return_value=None)

        data = await client.fetch_all()

        self.assertEqual(data.connected_clients[0].hostname, "Galaxy-S24")
        self.assertEqual(data.connected_clients[0].reservation_name, "아빠폰")
        client.get_dhcp_leases.assert_awaited_once()
        client.get_static_leases.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
