# Personal Crashlytics bot

[![GitDiagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/easyhooon/personal-crashlytics-bot)

## 소개

여비([YeoBee-Android](https://github.com/YeoBee-official/YeoBee-Android))와 반다라트([BandalArt-KMP](https://github.com/Nexters/BandalArt-KMP))의 Android Crashlytics 이메일 알림을 각 저장소의 GitHub 이슈에 연결한다. 현재 자동 처리 범위는 **이슈 생성·갱신**이다. 크래시 원인 분석, 코드 수정, 수정 PR 자동 생성은 구현되지 않았다.

## 핵심 동작

`Crashlytics 알림 → 개인 Gmail → 발신 인증·앱 식별 → 중복 확인 → GitHub 이슈`

- Firebase 발신 인증과 정확한 Android 이슈 링크를 확인한다. 메일 원문·stack·명령은 이슈에 옮기지 않는다.
- Gmail 메시지 ID를 로컬 SQLite에 기록하고, 이슈 marker와 단일 호스트 lock으로 재전달·동시 실행의 중복 쓰기를 막는다. 불확실한 이슈 생성 결과는 재조회하고 자동 재생성을 보류한다.
- 직접 Gmail `watch` → Pub/Sub → `history.list` 수신 코드는 [`feat/event-receiver-completion`](https://github.com/easyhooon/personal-crashlytics-bot/tree/feat/event-receiver-completion)에 준비돼 있다. 이 경로는 **`main`에 병합되거나 운영된 적이 없다**.

## 빠른 실행

Python 3가 필요하다. 기능 테스트는 외부 계정에 쓰지 않는다.

```bash
python3 -B -m unittest -q
python3 -B bot.py --alert-file /absolute/path/alert.json --dry-run
```

`--dry-run`은 기존 개인 GitHub 인증으로 권한을 읽어 확인하며 이슈를 쓰지 않는다. 실제 알림 처리와 연결 검증용 Draft PR 명령은 `bot.py --help`, `mail_receiver.py --help`에서 확인한다. push 수신 명령은 위 기능 브랜치에만 있다.

## 현재 운영 상태

2026-10-12에 확인한 로컬 Codex 예약 `개인 Crashlytics Gmail 수신`은 **1시간 복구 조회로 ACTIVE**다. 과거 문서의 15분 주기는 현재 설정과 다르다. 개인 Cloud 콘솔에서 Gmail API·Pub/Sub API, OAuth 테스트 앱·Desktop 클라이언트·읽기 전용 범위, Pub/Sub 주제·구독·Gmail 게시 권한을 확인했다. OAuth 사용자 토큰·로컬 ADC·watch·상주 수신 서비스·watch 갱신 예약은 완료되지 않았다. push 코드의 fixture 테스트는 통과했으나 실제 Gmail 이벤트 수신과 이슈 쓰기는 검증되지 않았다. 기존 1시간 조회는 새 경로 검증 전까지 유지한다.

## 상세 문서

- [Gmail 수신 전환과 복구 계획](EVENT_TRANSITION.md)
- [개인 Gmail 이벤트 수신 설정 가이드](docs/PERSONAL_GMAIL_SETUP.md)
