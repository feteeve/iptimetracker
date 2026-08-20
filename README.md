# ipTIME Tracker for Home Assistant

ipTIME 공유기에 접속된 기기를 Home Assistant에서 추적하는 커스텀 인테그레이션입니다.

## 기능

- **무선 접속 기기 추적**: `device_tracker` 엔티티로 WiFi 접속 기기 실시간 감지 (2.4GHz / 5GHz / 6GHz)
- **DHCP 임대 목록**: 현재 IP를 할당받은 기기 목록과 만료 시간
- **고정IP(Static DHCP) 목록**: 공유기에 설정된 MAC→IP 고정 매핑 정보
- **RSSI(신호 세기)** 속성 포함

## 설치 (HACS)

1. HACS → 사용자 정의 저장소 추가
2. URL: `https://github.com/feteeve/iptimetracker`
3. 카테고리: Integration
4. 설치 후 Home Assistant 재시작
5. 설정 → 통합 구성요소 → ipTIME Tracker 추가

## Home Assistant 없이 먼저 테스트

공유기 로그인과 접속자 조회를 빠르게 확인하려면 PC에서 독립 실행형 테스트 앱을 실행하세요.

```bash
python tools/iptime_probe.py
```

창에 공유기 주소, 관리자 아이디, 비밀번호를 입력하고 **로그인 및 접속자 조회**를 누르면 됩니다.
먼저 **수정 전 방식**을 선택하면 초기 인테그레이션과 동일한 로그인 요청을 재현할 수 있습니다.
터미널에서 실행하려면 다음 명령을 사용하세요. 비밀번호를 명령에 쓰지 않으면 화면에 표시되지 않게 입력받습니다.

```bash
python tools/iptime_probe.py --cli --host 192.168.0.1 --username admin
```

## 설정

| 항목 | 설명 | 기본값 |
|------|------|--------|
| 공유기 IP | 관리자 페이지 IP | `192.168.0.1` |
| 관리자 아이디 | 공유기 관리자 ID | `admin` |
| 관리자 비밀번호 | 공유기 관리자 PW | - |

## 생성되는 엔티티

- `device_tracker.{hostname}` — 무선 접속 기기마다 1개 (home/not_home)
- `sensor.iptime_무선_접속_기기_수` — 현재 WiFi 접속 기기 수
- `sensor.iptime_dhcp_임대_수` — DHCP 임대 기기 수
- `sensor.iptime_고정ip_설정_수` — 고정IP 설정 수

## 주의사항

- HA 서버와 ipTIME 공유기가 **같은 네트워크**에 있어야 합니다.
- 이지메시(EasyMesh) 환경에서는 **마스터 노드** IP를 입력하세요.
- 공유기 펌웨어 버전에 따라 파싱 결과가 다를 수 있습니다.
