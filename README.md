# Personal Crashlytics bot

[![GitDiagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/easyhooon/personal-crashlytics-bot)

## 소개

여비([YeoBee-Android](https://github.com/YeoBee-official/YeoBee-Android))와 반다라트([BandalArt-KMP](https://github.com/Nexters/BandalArt-KMP))의 Android Crashlytics 이메일 알림을 각 저장소의 GitHub 이슈에 연결한다. 현재 운영 자동 처리 범위는 **이슈 생성·갱신**이다. LLM 원인 분석, 코드 수정, 수정 PR 생성은 운영 수신기에 연결되지 않았다.

## 핵심 동작

`Crashlytics 알림 → 개인 Gmail → 발신 인증·앱 식별 → 중복 확인 → GitHub 이슈`

- Firebase 발신 인증과 정확한 Android 이슈 링크를 확인한다. 메일 원문·stack·명령은 이슈에 옮기지 않는다.
- Gmail 메시지 ID를 로컬 SQLite에 기록하고, 이슈 marker와 단일 호스트 lock으로 재전달·동시 실행의 중복 쓰기를 막는다. 불확실한 이슈 생성 결과는 재조회하고 자동 재생성을 보류한다.
- 직접 Gmail `watch` → Pub/Sub → `history.list` 수신 코드 [gmail_push.py](gmail_push.py)가 운영 Mac의 LaunchAgent로 실행 중이다. 받은 history의 모든 페이지를 처리한 뒤 cursor를 저장하고 Pub/Sub를 ACK한다. 2026-10-13에는 여비 개발 앱의 셸 주입 테스트 알림 한 건이 이 경로로 이슈 #561까지 전달됐다. 운영 앱에서 자연 발생한 크래시의 자동 수정은 검증되지 않았다.

## 이슈에서 수정 PR까지의 경계

```mermaid
flowchart TD
    A[Crashlytics 알림] --> B[Gmail watch · Pub/Sub]
    B --> C[발신·앱 검증 / 중복 방지]
    C --> D[대상 저장소 GitHub 이슈]
    D -. 미연결: 분석 실행기 .-> E[Firebase MCP 이슈·샘플 이벤트 읽기]
    E -.-> F[Codex CLI 읽기 전용 코드·stack 분석]
    F -.-> G{앱 코드 원인과 수정 근거 확인?}
    G -. 근거 부족·외부 원인·테스트 크래시 .-> S[확인 사실·가설·추가 정보 기록 후 STOP]
    G -. 확인 .-> H[관련 소스 패치와 실패 회귀 테스트]
    H -.-> I{수정 후 회귀·실제 프로젝트 검사 통과?}
    I -. 실패·검증 불가 .-> S
    I -. 통과·변경 연관성 확인 .-> J[Draft PR]
```

실선은 현재 운영 중인 이슈 생성 경로다. 점선은 운영 수신기에 아직 연결되지 않은 분석·수정 경로다. 현재 LLM 검증은 여비 테스트 이슈 한 건에 대해 `codex exec`의 읽기 전용 sandbox와 Firebase CLI MCP의 이슈·이벤트 읽기 도구만 사용했다. 결과는 셸 주입 크래시의 `issue_only`였고 PR은 만들지 않았다. 자동 코드 수정·테스트·Draft PR 실행기는 아직 없다.

읽기 전용 실험의 LLM 입력은 허용된 Android 앱 ID와 Crashlytics 이슈 ID로 한정했고, 여비 `develop` checkout의 코드와 Firebase CLI MCP의 샘플 이벤트를 읽었다. 이벤트에 포함될 수 있는 로그·breadcrumb는 비공개 데이터로 취급한다. Gmail 원문, 토큰, 개인 주소는 프롬프트나 GitHub 이슈에 전달하지 않는다. `codex exec --sandbox read-only`의 구조화 출력 schema에서 `issue_only` 결과를 확인했다. GitHub 쓰기 경로는 별도로 `easyhooon` 계정과 `TARGETS`의 두 저장소를 확인한다. [수정 판단 함수](repair_policy.py)는 앱 소유 stack, 원인과 변경 파일의 직접 연관성, 수정 전 실패·수정 후 통과한 회귀 테스트, 실제 프로젝트 검사와 commit SHA를 요구하지만 아직 운영 실행기에서 호출하지 않는다. 제조사 한정이라는 이유만으로 배제하지 않으며 근거가 없거나 외부 원인·검증 실패면 이슈에 남기고 멈춘다. 상세 경계는 [수정 판단 문서](docs/REPAIR_EVIDENCE_GATE.md)에 있다.

## 빠른 실행

Python 3가 필요하다. 기능 테스트는 외부 계정에 쓰지 않는다.

```bash
python3 -B -m unittest -q
python3 -B bot.py --alert-file /absolute/path/alert.json --dry-run
```

`--dry-run`은 기존 개인 GitHub 인증으로 권한을 읽어 확인하며 이슈를 쓰지 않는다. 실제 알림 처리, 연결 검증용 Draft PR, push 수신 명령은 [운영 명령](docs/OPERATIONS.md)을 따른다.

## 현재 운영 상태

2026-10-13에 확인한 로컬 Codex 예약 `개인 Crashlytics Gmail 수신`은 **1시간 복구 조회로 ACTIVE**다. 과거 문서의 15분 주기는 현재 설정과 다르다. 개인 Cloud에서 Gmail API·Pub/Sub API, OAuth 테스트 앱·Desktop 클라이언트, Pub/Sub 주제·pull 구독·Gmail 게시 권한을 확인했다. Gmail·Pub/Sub 단일 범위 OAuth의 별도 프로세스 접근, Gmail watch 등록·일일 갱신, Mac 상주 수신기, 실제 테스트 알림의 history 조회·cursor 저장·ACK와 GitHub 이슈 생성을 확인했다. 시간별 복구 예약은 계속 유지 중이다.

## 상세 문서

- [운영 명령과 안전 경계](docs/OPERATIONS.md)
- [Gmail push 전환과 복구](EVENT_TRANSITION.md)
- [개인 Gmail 이벤트 수신 설정 가이드](docs/PERSONAL_GMAIL_SETUP.md)
