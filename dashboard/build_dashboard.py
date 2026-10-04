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

APPLY_STATUS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 포트 진단') | list %}
{% if not matches %}
ℹ️ 새 상세 진단 코드는 HA에 설치됐지만 아직 로드되지 않았습니다. HA 재시작 후 포트·기기·DHCP 상세가 표시됩니다.
{% endif %}
"""

OVERVIEW = """1. **지금 상태**에서 공유기 통신과 인터넷 연결을 확인합니다.
2. 문제가 있으면 **상세 진단 수집**을 누릅니다. 약 10초 뒤 결과가 갱신됩니다.
3. **연결 상세**에서 포트와 접속 기기를, **DHCP·설정**에서 주소와 설정을 확인합니다.
"""

SPEED_GUIDE = """**연결 속도** · `1 Gbps`처럼 표시되는 포트 연결 규격입니다. 인터넷 회선의 최대 속도나 속도 테스트 결과는 아닙니다.

**진단 당시 사용량** · 내려받음(↓)과 올림(↑)은 마지막 수집 약 10초 동안 실제 흐른 양입니다. `0.34 Mbps`는 약 `43 kB/s`입니다. 기기별 연결 속도와는 다른 값입니다.
"""

PORTS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 포트 진단') | list %}
{% set rows = matches[0].attributes.get('entries', []) if matches else [] %}
{% if not matches %}
통합 업데이트와 HA 재시작 후 포트 진단을 볼 수 있습니다.
{% elif not rows %}
수집된 포트 정보가 없습니다. **상세 진단 수집**을 눌러 주세요.
{% else %}
| 포트 | 연결 속도 | 당시 사용량 ↓ / ↑ | 오류 증가 |
|:--|:--|:--|--:|
{% for p in rows %}| {{ (p.type or '포트') | upper }} {{ p.port }} | {{ p.link_label or '확인 불가' }} | {{ ((p.rx_mbps | float) | round(2) | string) ~ ' / ' ~ ((p.tx_mbps | float) | round(2) | string) ~ ' Mbps' if p.rx_mbps is defined and p.rx_mbps is not none and p.tx_mbps is defined and p.tx_mbps is not none else '—' }} | {{ ((p.rx_crc_delta | int) + (p.rx_drop_delta | int) + (p.tx_collision_delta | int)) if p.rx_crc_delta is defined and p.rx_drop_delta is defined and p.tx_collision_delta is defined else '—' }} |
{% endfor %}

사용량은 마지막 진단 약 10초의 평균입니다. 오류 증가는 CRC·드롭·충돌의 합계이며, `—`는 측정값이 없다는 뜻입니다.
{% endif %}
"""

STATIONS = """{% set matches = states.sensor | selectattr('name', 'eq', 'ipTIME 접속 기기 진단') | list %}
{% set rows = matches[0].attributes.get('entries', []) if matches else [] %}
{% if not matches %}
통합 업데이트와 HA 재시작 후 접속 기기 상세를 볼 수 있습니다.
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
통합 업데이트와 HA 재시작 후 DHCP 목록이 표시됩니다.
{% else %}
{% set leases = lease_matches[0].attributes.get('entries', []) %}
{% set reserved = reserved_matches[0].attributes.get('entries', []) %}
**수동 할당 {{ reserved_matches[0].state }}개 · IP 숫자 순서**
{% if reserved %}
| IP | 기기 | MAC |
|:--|:--|:--|
{% for r in reserved %}| {{ r.ip }} | {{ (r.name or '설명 없음') | replace('|', '/') | replace('\n', ' ') }} | {{ r.mac }} |
{% endfor %}
{% else %}수동 할당 목록이 비어 있거나 아직 수집되지 않았습니다.
{% endif %}

**DHCP 임대 {{ lease_matches[0].state }}개 · IP 숫자 순서**
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
통합 업데이트와 HA 재시작 후 주요 설정을 볼 수 있습니다.
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

DHCP_ACTION = """**추가:** 통합 설정에서 **DHCP 수동 할당 추가**를 선택합니다. 접속 기기를 고르거나 MAC·IP를 직접 입력할 수 있습니다.

**수정·삭제:** **DHCP 수동 할당 수정 · 삭제**에서 기존 항목을 고릅니다. IP·설명 수정과 삭제가 분리되어 있으며 삭제에는 한 번 더 확인이 필요합니다.

아래 버튼을 누르면 ipTIME Tracker 통합 설정으로 이동합니다.
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
                            tile("sensor.iptime_wan_ringkeu_sogdo", "WAN 포트 연결 속도"),
                            tile("sensor.iptime_ijimesi_wiseong_gigi_su", "EasyMesh 접속 기기")),
                    section("상세 진단", collect, markdown(SNAPSHOT), {**markdown(APPLY_STATUS), "show_empty": False}, markdown(OVERVIEW)),
                    section("마지막 진단 당시 WAN 사용량",
                            tile("sensor.iptime_wan_susin_teuraepig", "진단 당시 내려받음 사용량"),
                            tile("sensor.iptime_wan_songsin_teuraepig", "진단 당시 올림 사용량"),
                            markdown(SPEED_GUIDE)),
                ],
            },
            {
                "title": "연결 상세",
                "path": "connections",
                "icon": "mdi:lan-connect",
                "type": "sections",
                "max_columns": 1,
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
                "max_columns": 1,
                "sections": [
                    section("결과 시각", markdown(SNAPSHOT)),
                    section("IP 임대와 수동 할당", markdown(DHCP)),
                    section("수동 할당 관리", markdown(DHCP_ACTION), open_settings),
                    section("주요 설정", markdown(SETTINGS)),
                ],
            },
        ],
    }


if __name__ == "__main__":
    output = Path(__file__).with_name("iptime_diagnostics.json")
    output.write_text(json.dumps(build_dashboard(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
