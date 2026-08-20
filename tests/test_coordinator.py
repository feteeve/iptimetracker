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
            side_effect=[coordinator.UpdateFailed("unsupported"), []]
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


if __name__ == "__main__":
    unittest.main()
