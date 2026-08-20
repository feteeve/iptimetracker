# ipTIME Tracker for Home Assistant

ipTIME 공유기에 접속된 기기를 Home Assistant에서 추적하는 커스텀 인테그레이션입니다.

## 기능

- **무선 접속 기기 추적**: `device_tracker` 엔티티로 WiFi 접속 기기 실시간 감지 (2.4GHz / 5GHz / 6GHz)
- **3가지 공유기 UI 자동 감지**: 신형(Beta/Flutter) · 모바일(IUX) · 구형 UI를 자동으로 판별해 접속합니다. 구형 로그인 핸들러는 두 종류(`login_handler.cgi` / `login.cgi`)를 모두 시도합니다.
- **EasyMesh 위성 기기 지원**: 메시 환경에서 마스터에 직접 안 보이는 위성 기기도 토폴로지 API로 조회해 추적합니다.
- **재실 판정 디바운스**: 기기가 잠깐 접속자 목록에서 빠져도(절전모드 등) 설정한 시간 동안은 "재실"로 유지해 상태가 깜빡이는 것을 방지합니다.
- **DHCP 임대 목록**: 현재 IP를 할당받은 기기 목록과 만료 시간
- **고정IP(Static DHCP) 목록**: 공유기에 설정된 MAC→IP 고정 매핑 정보
- **RSSI(신호 세기)** 속성 포함 (EasyMesh 위성 기기는 RSSI 기준치 미만이면 재실로 보지 않음)

## 설치 (HACS)

1. HACS → 사용자 정의 저장소 추가
2. URL: `https://github.com/feteeve/iptimetracker`
3. 카테고리: Integration
4. 설치 후 Home Assistant 재시작
5. 설정 → 통합 구성요소 → ipTIME Tracker 추가 (같은 네트워크에 있으면 SSDP로 자동 검색되기도 합니다)

## 설정

| 항목 | 설명 | 기본값 |
|------|------|--------|
| 공유기 주소 | 관리자 페이지 IP (SSDP로 자동탐색되면 미리 채워짐) | `192.168.0.1` |
| 관리자 아이디 | 공유기 관리자 ID | `admin` |
| 관리자 비밀번호 | 공유기 관리자 PW | - |
| 외출 판단 지연 시간(초) | 접속자 목록에서 사라진 뒤에도 재실로 유지하는 시간 | `180` |
| 메쉬 기기 RSSI 기준치(dBm) | EasyMesh 위성 기기의 재실 판정 신호 기준 | `-90` |

설치 후에도 통합 구성요소 카드의 **설정(Configure)** 버튼으로 지연 시간/RSSI 기준치를 언제든 바꿀 수 있습니다.

## 생성되는 엔티티

- `device_tracker.{hostname}` — 무선/메쉬 접속 기기마다 1개 (home/not_home)
- `sensor.iptime_연결_기기_수` — 유무선 전체 접속 기기 수
- `sensor.iptime_무선_접속_기기_수` — 현재 WiFi 접속 기기 수
- `sensor.iptime_dhcp_임대_수` — DHCP 임대 기기 수
- `sensor.iptime_고정ip_설정_수` — 고정IP 설정 수

## 주의사항

- HA 서버와 ipTIME 공유기가 **같은 네트워크**에 있어야 합니다.
- 이지메시(EasyMesh) 환경에서는 **마스터 노드** IP를 입력하세요. 위성 노드에 붙은 기기는 자동으로 함께 조회됩니다.
- 공유기 펌웨어 버전에 따라 파싱 결과가 다를 수 있습니다.
