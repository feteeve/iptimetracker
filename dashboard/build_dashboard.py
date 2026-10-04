"""Build the built-in-card ipTIME diagnostic dashboard for Home Assistant."""

from __future__ import annotations

import json
from pathlib import Path


SNAPSHOT = """{% set stamp = state_attr('sensor.iptime_neteuweokeu_jindan', 'last_success') %}
{% if stamp %}
{% set age = (as_timestamp(now()) - as_timestamp(stamp)) | int %}
{% if age > 86400 %}⚠️ **지난 진단 결과** · {% else %}✅ **마지막 진단** · {% endif %}
{{ as_timestamp(stamp) | timestamp_custom('%m/%d %H:%M', true) }} ({{ relative_time(as_datetime(stamp)) }} 전)
{% else %}
ℹ️ **아직 상세 진단을 수집하지 않았습니다.** 아래 버튼을 누르면 새 결과가 표시됩니다.
{% endif %}
"""

OVERVIEW = """### 이 화면에서 확인하는 순서
1. **공유기 통신**과 **WAN 연결**을 먼저 확인합니다. 둘 다 현재 30초 조회 결과입니다.
2. 문제가 있거나 자세히 보고 싶을 때 **상세 진단 수집**을 누릅니다. 수집에는 약 10초가 걸립니다.
3. 결과 시각을 확인하고 **연결 상세**, **DHCP·설정** 탭을 봅니다.

WAN 링크 속도는 포트가 협상한 속도이고, WAN 송수신 Mbps는 진단 구간에 실제 흐른 양입니다.
"""

PORTS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 포트 진단') | list %}
{% set rows = matches[0].attributes.get('entries', []) if matches else [] %}
{% if not matches %}
2.3.1 업데이트 후 포트 진단을 볼 수 있습니다.
{% elif not rows %}
수집된 포트 정보가 없습니다. **상세 진단 수집**을 눌러 주세요.
{% else %}
{% for p in rows %}
**{{ (p.type or '포트') | upper }} {{ p.port }}** · 링크 {{ p.link if p.link not in [none, '', 'null'] else '끊김/미확인' }}
{% if p.rx_mbps is defined %}↓ {{ p.rx_mbps }} Mbps · ↑ {{ p.tx_mbps }} Mbps · 새 CRC {{ p.rx_crc_delta }} · 드롭 {{ p.rx_drop_delta }} · 충돌 {{ p.tx_collision_delta }}{% else %}트래픽 측정값 없음{% endif %}

{% endfor %}
측정 Mbps는 마지막 진단의 약 10초 구간 평균입니다. 링크 숫자는 포트 협상 속도입니다.
{% endif %}
"""

STATIONS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 접속 기기 진단') | list %}
{% set rows = matches[0].attributes.get('entries', []) if matches else [] %}
{% if not matches %}
2.3.1 업데이트 후 접속 기기 상세를 볼 수 있습니다.
{% elif not rows %}
수집된 접속 기기가 없습니다. **상세 진단 수집**을 눌러 주세요.
{% else %}
| 기기 | IP | 연결 | 신호 |
|:--|:--|:--|--:|
{% for d in rows %}
| {{ (d.name or d.mac or '이름 없음') | string | replace('|', '/') | replace('\n', ' ') }} | {{ d.ip or '—' }} | {{ d.band or d.connection or '—' }} | {{ (d.rssi_dbm | string) ~ ' dBm' if d.rssi_dbm is not none else '—' }} |
{% endfor %}

이 목록은 **진단 버튼을 누른 순간의 실제 접속 기기**입니다. RSSI는 무선 신호 세기이며 연결 속도가 아닙니다.
{% endif %}
"""

MESH = """{% set agents = state_attr('sensor.iptime_ijimesi_jindan', 'agents') or [] %}
**공유기 역할:** {{ states('sensor.iptime_ijimesi_jindan') }}
{% if agents %}
{% for a in agents %}
- **{{ a.nickname or a.product_name or a.mac or '에이전트' }}** · 상태 {{ a.status or '미확인' }} · 백홀 {{ a.backhaul or a.connection or '속도 정보 없음' }}
{% endfor %}
{% else %}
진단에서 확인된 에이전트 정보가 없습니다.
{% endif %}
에이전트의 숫자 연결 속도는 공유기가 값을 제공할 때만 확인할 수 있습니다.
"""

DHCP = """{% set lease_matches = states.sensor | selectattr('name', 'eq', 'ipTIME DHCP 임대 수') | list %}
{% set reserved_matches = states.sensor | selectattr('name', 'eq', 'ipTIME DHCP 수동 할당 수') | list %}
{% if not lease_matches or not reserved_matches %}
2.3.0 이상으로 업데이트하면 DHCP 목록이 표시됩니다.
{% else %}
{% set leases = lease_matches[0].attributes.get('entries', []) %}
{% set reserved = reserved_matches[0].attributes.get('entries', []) %}
**수동 할당 {{ reserved_matches[0].state }}개**
{% if reserved %}
| IP | 기기 | MAC |
|:--|:--|:--|
{% for r in reserved %}| {{ r.ip }} | {{ (r.name or '설명 없음') | replace('|', '/') | replace('\n', ' ') }} | {{ r.mac }} |
{% endfor %}
{% else %}수동 할당 목록이 비어 있거나 아직 수집되지 않았습니다.
{% endif %}

**DHCP 임대 {{ lease_matches[0].state }}개**
{% if leases %}
| IP | 기기 | MAC |
|:--|:--|:--|
{% for r in leases %}| {{ r.ip }} | {{ (r.name or '이름 없음') | replace('|', '/') | replace('\n', ' ') }} | {{ r.mac }} |
{% endfor %}
{% else %}임대 목록이 비어 있거나 아직 수집되지 않았습니다.
{% endif %}

임대 기록은 **현재 접속 중이라는 뜻이 아닙니다.** 실제 접속 여부는 연결 상세 탭에서 확인하세요.
{% endif %}
"""

SETTINGS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 주요 설정 진단') | list %}
{% set e = matches[0] if matches else none %}
{% if not e %}
2.3.1 업데이트 후 주요 설정을 볼 수 있습니다.
{% elif e.state in ['unavailable', 'unknown'] %}
확인 가능한 설정값이 없습니다. **상세 진단 수집**을 눌러 주세요.
{% else %}
{% for group, label in [('lan', 'LAN'), ('dhcp', 'DHCP'), ('dhcp_status', 'DHCP 상태'), ('wireless_channel', '무선 채널')] %}
{% set values = e.attributes.get(group) %}
{% if values %}
**{{ label }}**
{% for key, value in values.items() %}
- {{ key }}: {{ value }}
{% endfor %}
{% endif %}
{% endfor %}
공유기가 제공하지 않는 필드는 표시하지 않습니다. 이 화면에서는 설정을 변경하지 않습니다.
{% endif %}
"""

DHCP_ACTION = """### 수동 할당 추가
아래 **통합 설정 열기**를 누른 뒤 **설정 → DHCP 수동 할당 추가**를 선택하세요.
현재 접속 기기를 고르면 MAC·IP·이름이 미리 채워집니다. 저장 전에 IP를 확인해 주세요.
"""


def markdown(content: str, title: str | None = None) -> dict:
    card = {"type": "markdown", "content": content}
    if title:
        card["title"] = title
    return card


def tile(entity: str, name: str) -> dict:
    return {"type": "tile", "entity": entity, "name": name}


def section(title: str, *cards: dict) -> dict:
    return {"type": "grid", "title": title, "cards": list(cards)}


def build_dashboard() -> dict:
    collect = tile("button.iptime_jeonce_jeongbo_sujib", "상세 진단 수집")
    collect["tap_action"] = {
        "action": "perform-action",
        "perform_action": "button.press",
        "target": {"entity_id": "button.iptime_jeonce_jeongbo_sujib"},
    }
    open_settings = {
        "type": "button",
        "name": "통합 설정 열기",
        "icon": "mdi:ip-network-outline",
        "tap_action": {
            "action": "navigate",
            "navigation_path": "/config/integrations/integration/iptimetracker",
        },
    }
    return {
        "title": "ipTIME 진단",
        "views": [
            {
                "title": "한눈에",
                "path": "overview",
                "icon": "mdi:view-dashboard-outline",
                "type": "sections",
                "max_columns": 2,
                "sections": [
                    section("지금 상태",
                            tile("binary_sensor.iptime_gongyugi_tongsin_sangtae", "HA ↔ 공유기 통신"),
                            tile("binary_sensor.iptime_inteones_wan_yeongyeol", "공유기 ↔ 인터넷 링크"),
                            tile("sensor.iptime_wan_ringkeu_sogdo", "WAN 링크 속도"),
                            tile("sensor.iptime_ijimesi_wiseong_gigi_su", "EasyMesh 접속 기기")),
                    section("필요할 때 수집", collect, markdown(SNAPSHOT), markdown(OVERVIEW)),
                    section("마지막 수집의 핵심 수치",
                            tile("sensor.iptime_wan_susin_teuraepig", "WAN 수신 Mbps"),
                            tile("sensor.iptime_wan_songsin_teuraepig", "WAN 송신 Mbps"),
                            tile("sensor.iptime_jeongbo_sujib_beomwi", "지원 API 수")),
                ],
            },
            {
                "title": "연결 상세",
                "path": "connections",
                "icon": "mdi:lan-connect",
                "type": "sections",
                "max_columns": 2,
                "sections": [
                    section("결과 시각", markdown(SNAPSHOT)),
                    section("포트별 링크·트래픽·오류", markdown(PORTS)),
                    section("실제 접속 기기", markdown(STATIONS)),
                    section("EasyMesh 에이전트", markdown(MESH)),
                ],
            },
            {
                "title": "DHCP·설정",
                "path": "addresses",
                "icon": "mdi:ip-network-outline",
                "type": "sections",
                "max_columns": 2,
                "sections": [
                    section("결과 시각", markdown(SNAPSHOT)),
                    section("IP 임대와 수동 할당", markdown(DHCP)),
                    section("수동 할당 추가", markdown(DHCP_ACTION), open_settings),
                    section("주요 설정", markdown(SETTINGS)),
                ],
            },
        ],
    }


if __name__ == "__main__":
    output = Path(__file__).with_name("iptime_diagnostics.json")
    output.write_text(json.dumps(build_dashboard(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
