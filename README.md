# Personal Crashlytics bot

[![GitDiagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/easyhooon/personal-crashlytics-bot)

## 소개

여비([YeoBee-Android](https://github.com/YeoBee-official/YeoBee-Android))와 반다라트([BandalArt-KMP](https://github.com/Nexters/BandalArt-KMP))의 Android Crashlytics 이메일 알림을 각 저장소의 GitHub 이슈에 연결한다. 현재 운영 자동 처리 범위는 **이슈 생성·갱신**이다. LLM 원인 분석, 코드 수정, 수정 PR 생성은 운영 수신기에 연결되지 않았다.

## 핵심 동작

`Crashlytics 알림 → 개인 Gmail → 발신 인증·앱 식별 → 중복 확인 → GitHub 이슈`

- Firebase 발신 인증과 정확한 Android 이슈 링크를 확인한다. 메일 원문·stack·명령은 이슈에 옮기지 않는다.
- Gmail 메시지 ID를 로컬 SQLite에 기록하고, 이슈 marker와 단일 호스트 lock으로 재전달·동시 실행의 중복 쓰기를 막는다. 불확실한 이슈 생성 결과는 재조회하고 자동 재생성을 보류한다.
- 직접 Gmail `watch` → Pub/Sub → `history.list` 수신 코드 [gmail_push.py](gmail_push.py)가 운영 Mac의 LaunchAgent로 실행 중이다. 받은 history의 모든 페이지를 처리한 뒤 cursor를 저장하고 Pub/Sub를 ACK한다. 2026-10-13에는 여비 개발 앱의 셸 주입 테스트 알림 한 건이 이 경로로 이슈 #561까지 전달됐다. 운영 앱에서 자연 발생한 크래시의 자동 수정은 검증되지 않았다.

## 아키텍처

```mermaid
flowchart TD
    subgraph MAC["운영 Mac · 이 저장소"]
        A["Crashlytics 알림 메일"] --> B["gmail_push.py<br/>Gmail watch · Pub/Sub 수신"]
        B --> C["bot.py<br/>발신·앱 검증 · 중복 방지"]
        W["repair_worker.py<br/>SQLite 작업 큐"]
        T{"repair_triage.py<br/>셸 주입 테스트 크래시?"}
        G{"repair_project.py<br/>앱 stack·예외·라인과 패치 대조"}
        V{"repair_ci.py<br/>A 실패 · B 통과 · SHA 일치?"}
        P["repair_publish.py<br/>커밋 B 그대로 Draft PR"]
    end

    subgraph LLM["🤖 LLM 개입 · Codex CLI 읽기 전용"]
        F["Firebase MCP로 이슈·샘플 이벤트 읽기<br/>원인 설명 + 회귀 테스트 패치 + 수정 패치 제안<br/>또는 no_code_fix"]
    end

    subgraph GH["GitHub · 대상 저장소"]
        D["GitHub 이슈"]
        subgraph AD["어댑터: .github/workflows/crash-repair-verify.yml"]
            YB["YeoBee-Android<br/>ubuntu · ktlint · detekt · testDebugUnitTest"]
            BA["BandalArt-KMP<br/>macOS · spotless · detekt · allTests"]
        end
        PR["Draft PR<br/>사람 리뷰 후 Ready → 기존 CI"]
    end

    C --> D
    C -. "--repair-config 시" .-> W
    W -.-> T
    T -. 예 .-> S["이슈에 판단 기록 후 STOP"]
    T -. 아니오 .-> F
    F -. no_code_fix .-> S
    F -. 패치 제안 .-> G
    G -. 불일치 .-> S
    G -. "커밋 A·B를 crash-verify/** 로 push" .-> AD
    AD -. "run 결과 · JUnit XML" .-> V
    V -. 실패 .-> S
    V -. 통과 .-> P --> PR

    classDef llm fill:#fde68a,stroke:#b45309,color:#000
    class F llm
```

실선은 운영 중인 이슈 생성 경로이고, 점선은 구현했지만 운영에서 켜지 않은(`--repair-config` 없음) 수정 경로다.

### LLM이 개입하는 곳
- **딱 한 번**: 셸 주입 테스트가 아닌 크래시에서 원인을 분석하고 패치를 *제안*할 때다(`codex_repair.py`)
- 입력은 앱 ID·Crashlytics 이슈 ID·대상 checkout뿐이다. Gmail 원문과 토큰은 넘기지 않는다
- 읽기 전용으로 실행한다. 파일 수정, 명령 실행, GitHub 쓰기를 하지 않는다
- 판단은 코드가 한다. stack 대조, 테스트 실패·통과, SHA 일치를 확인하고, 모델의 확신이나 "테스트 통과" 주장은 근거로 쓰지 않는다

### 대상 저장소 어댑터
봇은 앱과 무관하게 같은 절차로 동작한다. 그 저장소를 어떻게 빌드하고 테스트하는지는 각 저장소의 `crash-repair-verify.yml`이 맡는다.

| 저장소 | 어댑터 | 실행 |
|---|---|---|
| YeoBee-Android | [#563](https://github.com/YeoBee-official/YeoBee-Android/pull/563) | ubuntu, `testDebugUnitTest ktlintCheck detekt :app:assembleDebug` |
| BandalArt-KMP | [#438](https://github.com/Nexters/BandalArt-KMP/pull/438) | macOS, `spotlessCheck`(변경분) `detekt allTests :androidApp:testDebugUnitTest :androidApp:assembleDebug` |

- 봇과의 약속: `crash-verify/**` push에서 실행, 이 파일 경로, JUnit XML을 `test-results` artifact로 업로드
- 안전: 시크릿 없음(더미 keystore), `contents: read`, checkout에 토큰 안 남김. 기존 CI는 Draft PR에서 돌지 않는다
- AI 패치는 `.github`를 못 바꾼다. 검사 항목을 바꾸려면 어댑터를 사람이 리뷰해서 고친다
- 앱 추가: 어댑터 파일 1개 + 봇의 `bot.TARGETS`·`ANDROID_PACKAGES`·`test_contract` 설정

### 검증 상태
- 실제 Actions에서 성공·실패 경로 smoke 확인([판단 문서](docs/REPAIR_EVIDENCE_GATE.md)에 run 링크)
- 실제 앱 크래시로 Draft PR까지 간 종단 간 검증은 아직 없다
- 자동 병합은 하지 않는다

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
