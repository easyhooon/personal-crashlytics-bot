# Personal Crashlytics bot

[![GitDiagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/easyhooon/personal-crashlytics-bot)

## 소개

개인 Android 앱의 Crashlytics 이메일 알림을 해당 앱 저장소의 GitHub 이슈에 연결한다. 현재 자동 처리 범위는 **이슈 생성·갱신**이다. 크래시 원인 분석, 코드 수정, 수정 PR 자동 생성은 구현되지 않았다.

## 핵심 동작

`Crashlytics 알림 → 개인 Gmail → 발신 인증·앱 식별 → 중복 확인 → GitHub 이슈`

- Firebase 발신 인증과 정확한 Android 이슈 링크를 확인한다. 메일 원문·stack·명령은 이슈에 옮기지 않는다.
- Gmail 메시지 ID를 로컬 SQLite에 기록하고, 이슈 marker와 단일 호스트 lock으로 재전달·동시 실행의 중복 쓰기를 막는다. 불확실한 이슈 생성 결과는 재조회하고 자동 재생성을 보류한다.
- 직접 Gmail `watch` → Pub/Sub → `history.list` 수신 코드는 준비돼 있다. 받은 history의 모든 페이지를 처리한 뒤 cursor를 저장하고 Pub/Sub를 ACK한다. 이 경로는 **아직 운영되지 않는다**.

## 빠른 실행

Python 3가 필요하다. 기능 테스트는 외부 계정에 쓰지 않는다.

```bash
python3 -B -m unittest -q
python3 -B bot.py --alert-file /absolute/path/alert.json --dry-run
```

`--dry-run`은 기존 개인 GitHub 인증으로 권한을 읽어 확인하며 이슈를 쓰지 않는다. 실제 알림 처리, 연결 검증용 Draft PR, push 수신 명령은 [운영 명령](docs/OPERATIONS.md)을 따른다.

## 현재 운영 상태

2026-10-12에 확인한 로컬 Codex 예약 `개인 Crashlytics Gmail 수신`은 **1시간 복구 조회로 ACTIVE**다. 과거 문서의 15분 주기는 현재 설정과 다르다. 새 Gmail watch, Pub/Sub 구독, 상주 수신 서비스, watch 갱신 예약은 활성화되지 않았다. 준비한 push 코드의 fixture 테스트는 통과했지만 실제 OAuth·Cloud 자원·운영 호스트 수신은 아직 검증되지 않았다. 기존 1시간 조회는 새 경로 검증 전까지 유지한다.

## 상세 문서

- [운영 명령과 안전 경계](docs/OPERATIONS.md)
- [Gmail push 전환과 복구](EVENT_TRANSITION.md)
