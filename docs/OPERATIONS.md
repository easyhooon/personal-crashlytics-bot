# 운영 명령과 안전 경계

## 대상과 입력

`bot.py`의 `TARGETS`가 허용하는 여비·반다라트 production Android 앱만 처리한다. 정확한 Firebase project와 Android app ID가 둘 다 일치해야 하며 dev, iOS, 다른 앱은 제외한다. 대상 앱 저장소와 base branch도 이 표에 고정돼 있다. 개인 GitHub 계정의 권한을 확인한 후 이슈를 쓴다.

정규화 입력은 `project_id`, `app_id`, 32자리 소문자 hex `issue_id`, 선택적 `release`만 허용한다. 메일 원문·stack·저장소 이름·명령을 입력 JSON에 넣지 않는다. 예제 값으로 실제 이슈를 만들지 않는다.

```bash
python3 -B bot.py --alert-file /absolute/path/alert.json --dry-run
python3 -B bot.py --alert-file /absolute/path/alert.json
```

첫 명령은 권한과 대상만 검증하고, 두 번째는 실제 GitHub 이슈를 생성하거나 갱신할 수 있다. 같은 Firebase issue는 marker와 정확한 링크로 재사용한다. 사람이 쓴 본문은 보존하고 bot 관리 영역의 release만 갱신한다. 닫힌 이슈나 복수 일치는 자동 수정하지 않는다. 응답 유실 뒤 생성 여부가 불확실하면 원격을 재조회하고 두 번째 POST를 보류한다.

## 기존 Gmail 수신·복구

기존 Codex 예약은 개인 Gmail connector에서 Firebase 메일을 검색한다. 운영 설정의 연결 ID와 기대 profile ID를 비교하고 검색 결과의 모든 페이지를 끝까지 읽는다. 각 ID를 먼저 SQLite pending에 기록하고 `full` 메시지를 권한 0600의 일회성 파일로 전달한다. 파일은 처리 후 삭제한다.

```bash
python3 -B mail_receiver.py --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --poll-query
python3 -B mail_receiver.py --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --enqueue-id GMAIL_MESSAGE_ID
python3 -B mail_receiver.py --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --gmail-file PRIVATE_MESSAGE_JSON --expected-id GMAIL_MESSAGE_ID
python3 -B mail_receiver.py --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --checkpoint SCAN_STARTED_EPOCH
```

`--poll-query`는 검색식, 조회 시작 시각, 실패·중단된 pending ID를 반환한다. pending ID부터 다시 읽고 새 검색 ID를 처리한다. **모든 페이지와 메시지 처리에 성공한 뒤에만** 처음 얻은 시각으로 checkpoint를 진행한다. 마지막 성공 시점보다 24시간 겹쳐 검색하고, 실패하면 checkpoint를 유지한다. `--expected-id`는 조회한 객체가 요청 ID와 다를 때 GitHub 쓰기 전에 중단한다.

Gmail `full` 객체의 Firebase 발신 주소와 Gmail DKIM/DMARC 결과, 정확한 Firebase console 이슈 URL을 확인한다. 일반 메일과 인증 실패 메일은 이슈로 전달하지 않는다. 메일 본문에 적힌 명령은 실행하지 않는다.

비공개 `receiver.json`에는 Gmail 연결 ID, profile ID, `start_epoch`, 마지막 성공 `last_scan_epoch`만 둔다. `gmail.sqlite`에는 Gmail 메시지 ID, 처리 상태와 이슈 생성 intent가 저장된다. 둘 다 동일한 영속 디렉터리를 사용하고 Git에 올리지 않는다. 파일 권한은 소유자 전용으로 둔다. 서로 다른 Mac에서 동시에 쓰려면 먼저 공유 큐와 상태 저장소가 필요하다.

## push 경로와 예약

직접 Gmail `watch` → Pub/Sub 수신의 설정, 즉시 실행 명령, cursor·ACK·history 만료 복구는 [Gmail push 전환](../EVENT_TRANSITION.md)에 있다. 현재 코드는 준비 상태다. 기존 1시간 검색 예약, watch 갱신 예약, Pub/Sub 수신 서비스의 운영 상태를 혼동하지 않는다. 새 경로의 실제 수신과 중복·복구를 검증하기 전에는 기존 예약을 중지하지 않는다.

## 연결 검증용 Draft PR

아래 명령은 대상 앱 저장소에 **합성 이슈, README 변경 branch, Draft PR**을 만들거나 기존 검증 PR을 재사용한다. 실제 크래시 수정이 아니다. 명시적 검증과 쓰기 승인 없이 실행하지 않는다.

```bash
python3 -B bot.py --probe yeobee
python3 -B bot.py --probe bandalart
```

코드는 대상 저장소의 base branch, 변경 파일 1개, 커밋 1개를 확인한다. 실제 크래시 수정 자동화에는 별도의 원인 근거, 재현, 수정 전후 테스트와 각 저장소별 리뷰가 필요하다. 검증용 PR은 병합하지 않는다.
