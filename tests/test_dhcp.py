from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


PATH = Path(__file__).parents[1] / "custom_components" / "iptimetracker" / "dhcp.py"
SPEC = importlib.util.spec_from_file_location("iptimetracker_dhcp", PATH)
assert SPEC and SPEC.loader
dhcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dhcp)


class DhcpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls = []
        self.reservations = []
        self.leases = [{"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.50"}]
        self.stations = []

        async def request(method, params=None, **kwargs):
            self.calls.append((method, params, kwargs))
            if method == "dhcpd/reservedaddr/show":
                return None, {"result": list(self.reservations)}
            if method == "dhcpd/lease/show":
                return None, {"result": list(self.leases)}
            if method == "network/interface/lan/info":
                return None, {"result": {"ip": "192.168.0.1", "mask": "255.255.255.0"}}
            if method == "network/interface/lan/stations":
                return None, {"result": list(self.stations)}
            if method == "dhcpd/reservedaddr/add":
                self.reservations.append({"mac": params["mac"], "ip": params["ip"], "desc": params["desc"]})
                return None, {"result": True}
            raise AssertionError(method)

        self.manager = dhcp.DhcpReservations(SimpleNamespace(_request_json=AsyncMock(side_effect=request)))

    async def test_list_shows_existing_reservation(self) -> None:
        self.reservations = [{"mac": "AA:BB:CC:DD:EE:02", "ip": "192.168.0.52", "desc": "TV"}]
        self.assertEqual((await self.manager.list())[0]["name"], "TV")

    async def test_add_checks_and_verifies(self) -> None:
        rows = await self.manager.add("aa-bb-cc-dd-ee-02", "192.168.0.52", "TV")
        self.assertEqual(rows, [{"mac": "AA:BB:CC:DD:EE:02", "ip": "192.168.0.52", "name": "TV"}])
        writes = [call for call in self.calls if call[0] == "dhcpd/reservedaddr/add"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2], {"retry_on_auth": False})

    async def test_rejects_lease_conflict_without_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "다른 기기"):
            await self.manager.add("AA:BB:CC:DD:EE:02", "192.168.0.50")
        self.assertFalse(any(call[0].endswith("/add") for call in self.calls))

    async def test_rejects_router_ip_without_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "LAN"):
            await self.manager.add("AA:BB:CC:DD:EE:02", "192.168.0.1")
        self.assertFalse(any(call[0].endswith("/add") for call in self.calls))

    async def test_rejects_unknown_lease_shape_without_write(self) -> None:
        self.leases = {"leases": []}
        with self.assertRaisesRegex(ValueError, "목록 형식"):
            await self.manager.add("AA:BB:CC:DD:EE:02", "192.168.0.52")
        self.assertFalse(any(call[0].endswith("/add") for call in self.calls))

    async def test_rejects_connected_conflict_without_write(self) -> None:
        self.stations = [{"mac": "AA:BB:CC:DD:EE:03", "info": {"ip": "192.168.0.52"}}]
        with self.assertRaisesRegex(ValueError, "접속 기기"):
            await self.manager.add("AA:BB:CC:DD:EE:02", "192.168.0.52")
        self.assertFalse(any(call[0].endswith("/add") for call in self.calls))

    async def test_does_not_retry_write_after_timeout(self) -> None:
        original = self.manager.client._request_json.side_effect
        attempts = 0

        async def timeout_write(method, params=None, **kwargs):
            nonlocal attempts
            if method == "dhcpd/reservedaddr/add":
                attempts += 1
                raise TimeoutError
            return await original(method, params, **kwargs)

        self.manager.client._request_json.side_effect = timeout_write
        with self.assertRaises(TimeoutError):
            await self.manager.add("AA:BB:CC:DD:EE:02", "192.168.0.52")
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
