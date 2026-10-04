from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).parents[1] / "custom_components" / "iptimetracker" / "diagnostic_view.py"
SPEC = importlib.util.spec_from_file_location("iptimetracker_diagnostic_view", PATH)
assert SPEC and SPEC.loader
view = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(view)


class DiagnosticViewTest(unittest.TestCase):
    def test_ports_merge_link_and_sample_without_extra_calls(self) -> None:
        snapshot = {
            "raw": {"port.links": [{"type": "lan", "port": 1, "link": "1000f"}]},
            "traffic": {"ports": [{"type": "lan", "port": 1, "rx_mbps": 3.2, "rx_crc_delta": 1}]},
        }
        self.assertEqual(view.ports(snapshot), [{
            "type": "lan", "port": "1", "link": "1000f", "link_label": "1 Gbps · 전이중", "rx_mbps": 3.2, "rx_crc_delta": 1,
        }])

    def test_link_labels_explain_router_codes(self) -> None:
        self.assertEqual(view.link_label("100h"), "100 Mbps · 반이중")
        self.assertEqual(view.link_label(None), "끊김/미확인")
        self.assertEqual(view.link_label("unexpected"), "확인 불가")

    def test_connected_stations_uses_actual_station_list(self) -> None:
        snapshot = {"raw": {"network.lan_stations": [{
            "mac": "AA:BB:CC:DD:EE:02",
            "info": {"ip": "192.168.0.52", "name": "TV"},
            "connection": {"type": "wireless", "wireless": {"bss": "5g.1", "rssi": -55}},
        }]}}
        self.assertEqual(view.connected_stations(snapshot)[0]["rssi_dbm"], -55)
        self.assertEqual(view.connected_stations({"raw": {"dhcp.leases": [{}]}}), None)

    def test_settings_include_only_allowlisted_fields(self) -> None:
        snapshot = {"raw": {
            "network.lan_info": {"ip": "192.168.0.1", "mask": "255.255.255.0", "password": "secret"},
            "dhcp.config": {"enabled": True, "start": "192.168.0.2", "password": "secret"},
        }}
        settings = view.selected_settings(snapshot)
        self.assertEqual(settings["lan"]["ip"], "192.168.0.1")
        self.assertNotIn("password", str(settings))


if __name__ == "__main__":
    unittest.main()
