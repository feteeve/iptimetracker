# ipTIME Tracker for Home Assistant

ipTIME 공유기의 관리자 페이지에서 현재 접속 기기를 읽어 Home Assistant의
`device_tracker` 엔티티로 제공하는 커스텀 인테그레이션입니다.

## 주요 기능

- 신형 Beta/Flutter, 모바일 IUX, 구형 UI 자동 감지
- 감지한 UI의 API가 작동하지 않으면 다른 로그인 방식으로 자동 전환
- 초기 버전에서 사용하던 `timepro.cgi` 로그인/조회 방식 지원
- 무선 2.4/5/6GHz, 유선/DHCP 및 EasyMesh 접속자 통합
- 세션 만료 감지, 자동 재로그인 및 1회 재시도
- 기기별 `device_tracker` 생성과 `consider_home` 재실 지연
- 알려진 기기와 마지막 접속 시각을 복원하여 재시작 중 외출한 기기도 유지
- 접속자, 무선 접속자, DHCP 및 고정 DHCP 개수 센서
- SSDP를 통한 ipTIME 공유기 자동 발견

고정 DHCP 목록은 해당 메뉴를 제공하는 펌웨어에서만 표시됩니다. 모바일 UI가
유선 목록을 제공하지 않는 펌웨어에서는 구형 DHCP 페이지를 함께 조회하여
보완하며, 해당 페이지도 없는 모델은 무선 접속자만 표시될 수 있습니다.

## 설치

1. HACS에서 사용자 정의 저장소를 추가합니다.
2. 저장소 URL에 `https://github.com/feteeve/iptimetracker`를 입력합니다.
3. 카테고리는 `Integration`을 선택합니다.
4. ipTIME Tracker를 다운로드한 뒤 Home Assistant를 재시작합니다.
5. 설정 → 기기 및 서비스 → 통합 구성요소 추가에서 `ipTIME Tracker`를 선택합니다.

## 설정값

| 항목 | 설명 | 기본값 |
|---|---|---|
| 공유기 주소 | 관리자 페이지 IP/호스트와 필요한 경우 포트 | `192.168.0.1` |
| 관리자 아이디 | 공유기 관리자 ID. 빈 계정이면 비워 둘 수 있음 | `admin` |
| 관리자 비밀번호 | 공유기 관리자 비밀번호. 비밀번호가 없으면 비워 둘 수 있음 | 빈 값 |
| 외출 판단 지연 | 목록에서 사라진 뒤에도 재실로 유지할 시간(0~86400초) | `180` |
| 메쉬 RSSI 기준 | EasyMesh 위성 기기의 최소 신호(-120~0dBm) | `-90` |

옵션은 통합 구성요소의 **설정** 버튼에서 변경할 수 있습니다. 옵션 변경 시 해당
통합 구성요소만 다시 로드되며 Home Assistant 전체를 재시작할 필요는 없습니다.

## 생성되는 엔티티

- 접속 기기별 `device_tracker` (`home` / `not_home`)
- 전체 접속 기기 수 센서
- 무선 접속 기기 수 센서
- DHCP 목록 수 센서
- 고정 DHCP 설정 수 센서

센서 속성에는 최대 100개의 상세 항목만 저장합니다. 항목이 더 많으면
`attributes_truncated`가 `true`가 되고 실제 개수는 `total_items`에서 확인할 수
있습니다. 이는 Home Assistant Recorder 데이터베이스가 과도하게 커지는 것을
방지하기 위한 제한입니다.

## 주의사항

- Home Assistant에서 공유기 관리자 주소로 접속할 수 있어야 합니다.
- EasyMesh 환경에서는 컨트롤러(마스터) 공유기의 주소를 입력하세요.
- CAPTCHA가 활성화된 관리자 계정은 자동 로그인이 불가능합니다.
- ipTIME 펌웨어별 관리자 페이지 형식 차이로 일부 보조 정보가 제공되지 않을 수
  있습니다. 문제 발생 시 Home Assistant 로그의 `iptimetracker` 항목을 확인하세요.
