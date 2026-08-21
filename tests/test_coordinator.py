from __future__ import annotations

import importlib.util
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

    def test_valid_ip_rejects_out_of_range_octets(self) -> None:
        self.assertEqual(coordinator.IptimeClient._valid_ip("999.1.1.1"), "")
        self.assertEqual(
            coordinator.IptimeClient._valid_ip("text 192.168.0.30 more"),
            "192.168.0.30",
        )

    def test_name_alias_fields_are_supported(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        self.assertEqual(
            client._name_from_mappings(
                "AA:BB:CC:DD:EE:FF", {"nickname": "거실 스피커"}
            ),
            "거실 스피커",
        )

    def test_clean_name_rejects_mac_ip_and_junk_values(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        self.assertEqual(client._clean_name("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"), "")
        self.assertEqual(client._clean_name("192.168.0.1", "AA:BB:CC:DD:EE:FF"), "")
        self.assertEqual(client._clean_name("wired", "AA:BB:CC:DD:EE:FF"), "")
        self.assertEqual(client._clean_name("거실 TV", "AA:BB:CC:DD:EE:FF"), "거실 TV")


class IptimeClientAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_request_raises_auth_error_without_retry(self) -> None:
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
            await client._login_request()
        self.assertFalse(client._request_json.call_args.kwargs["retry_on_auth"])

    async def test_login_request_raises_captcha_required(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._username = "admin"
        client._password = "correct"
        client._request_json = AsyncMock(
            return_value=(
                SimpleNamespace(url="http://router/", cookies={}),
                {"error": {"code": -31997, "message": "captcha required"}},
            )
        )
        with self.assertRaises(coordinator.IptimeCaptchaRequired):
            await client._login_request()

    async def test_login_request_succeeds_with_session_cookie(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._username = "admin"
        client._password = "correct"
        client._request_json = AsyncMock(
            return_value=(
                SimpleNamespace(url="http://router/", cookies={"efm_session_id": "abc"}),
                {"result": True},
            )
        )
        client._get_session = AsyncMock(
            return_value=SimpleNamespace(
                cookie_jar=SimpleNamespace(filter_cookies=lambda url: {})
            )
        )
        self.assertTrue(await client._login_request())
        self.assertTrue(client._logged_in)

    async def test_login_retries_once_before_succeeding(self) -> None:
        """A transient first-attempt failure gets one same-session retry."""
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._logged_in = False
        client._mesh_enabled = False
        client._base_url = "http://router"
        client._clear_session_cookies = AsyncMock()
        client._login_request = AsyncMock(
            side_effect=[coordinator.UpdateFailed("transient"), True]
        )
        client.get_wireless_clients = AsyncMock(return_value=[])
        client._detect_mesh_enabled = AsyncMock(return_value=False)

        self.assertTrue(await client.login())
        self.assertEqual(client._login_request.await_count, 2)
        client.get_wireless_clients.assert_awaited_once()

    async def test_login_raises_after_exhausting_retries(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._logged_in = False
        client._mesh_enabled = False
        client._base_url = "http://router"
        client._clear_session_cookies = AsyncMock()
        client._login_request = AsyncMock(
            side_effect=coordinator.UpdateFailed("router unreachable")
        )

        with self.assertRaises(coordinator.UpdateFailed):
            await client.login()
        self.assertEqual(client._login_request.await_count, 2)

    async def test_get_wireless_clients_parses_stations_payload(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._request_json = AsyncMock(
            return_value=(
                SimpleNamespace(),
                {
                    "result": [
                        {
                            "mac": "aa:bb:cc:dd:ee:ff",
                            "info": {"ip": "192.168.0.50", "name": "거실 TV"},
                            "connection": {
                                "type": "wireless",
                                "wireless": {"bss": "5g.1", "rssi": -55},
                            },
                        }
                    ]
                },
            )
        )
        clients = await client.get_wireless_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(clients[0].hostname, "거실 TV")
        self.assertEqual(clients[0].interface, "5GHz")
        self.assertEqual(clients[0].rssi, -55)

    async def test_get_wireless_clients_rejects_non_list_result(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._request_json = AsyncMock(
            return_value=(SimpleNamespace(), {"result": {"unexpected": "shape"}})
        )
        with self.assertRaises(coordinator.UpdateFailed):
            await client.get_wireless_clients()

    async def test_fetch_all_merges_mesh_clients_and_returns_empty_leases(self) -> None:
        client = coordinator.IptimeClient.__new__(coordinator.IptimeClient)
        client._logged_in = True
        client._mesh_enabled = True
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
        client.get_mesh_clients = AsyncMock(
            return_value=[
                coordinator.WirelessClient(
                    mac="11:22:33:44:55:66",
                    ip="",
                    hostname="11:22:33:44:55:66",
                    interface="메쉬-sta",
                    rssi=-60,
                )
            ]
        )
        client.get_wan_link_status = AsyncMock(return_value=None)

        data = await client.fetch_all()

        self.assertEqual(len(data.connected_clients), 2)
        self.assertEqual(data.dhcp_leases, [])
        self.assertEqual(data.static_leases, [])
        client.get_mesh_clients.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
