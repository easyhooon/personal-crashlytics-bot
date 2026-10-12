# 개인 Gmail 이벤트 수신 전환과 시간별 복구 조회

현재 운영 중인 것은 기존 Codex 1시간 Gmail 검색 복구 작업이다. 직접 Gmail API `watch` → Cloud Pub/Sub StreamingPull → `history.list` 경로는 코드와 fixture 테스트가 준비됐다. 개인 Gmail 읽기 전용 OAuth와 API `getProfile` 호출, Pub/Sub 주제·7일 pull 구독·Gmail 게시 권한을 확인했고 Pub/Sub 단일 범위 OAuth를 Keychain에 저장했다. Pub/Sub 자격증명의 별도 프로세스 구독 조회, watch, 상주 서비스, 갱신·복구 예약과 실제 알림 수신은 미완료다. 기존 복구 작업은 새 경로의 실제 수신을 확인할 때까지 유지한다.

## 입력과 처리

Pub/Sub 데이터는 Gmail 메시지 ID가 아닌 `{"emailAddress":"…","historyId":"…"}`다. 수신기는 설정된 개인 계정과 일치하는지 검사하고, SQLite에 저장한 직전 cursor부터 `history.list(historyTypes=messageAdded)`의 모든 페이지를 읽는다. 각 메시지 ID를 기존 SQLite pending에 저장한 다음 `From` 메타데이터만 조회한다. Firebase 발신 메일에 한해 Gmail `full` 객체를 읽고 기존 수신기의 DKIM/DMARC 인증과 두 production Android 앱의 정확한 이슈 URL을 검증해 GitHub 이슈를 동기화한다. 일반 메일은 본문을 읽지 않고 처리 ID만 기록한다. 메일 원문과 stack은 GitHub 이슈에 올리지 않는다.

모든 pending 재처리와 history 페이지가 끝나야 cursor를 전진시킨다. 그 뒤 Pub/Sub를 ACK한다. Gmail·GitHub·상태 저장 중 실패하면 NACK하며 cursor를 유지한다. callback은 메일 내용 없이 `pubsub: acked`와 처리 상태 또는 `pubsub: nacked`와 오류 종류만 기록한다. 재전송과 동시 callback은 같은 Mac의 작업 lock, SQLite 메시지 선점, 원격 이슈 marker로 중복 쓰기를 막는다. 프로세스가 쓰기 후 ACK 전에 종료돼도 다음 delivery가 저장된 cursor 또는 pending에서 재개한다. 이 보장은 한 Mac과 한 공유 상태 경로에 한정된다.

초기 `watch`는 현재 historyId를 받은 뒤 기존 겹침 검색을 완료하고 cursor를 저장한다. 따라서 검색 중 도착한 메일은 이후 history에서 다시 보인다. 이후 `watch` 갱신은 cursor를 덮어쓰지 않는다. 갱신은 메시지를 주기적으로 읽는 폴링이 아니라 구독 유지 호출이다. Gmail은 최소 7일마다 갱신을 요구하고 매일 갱신을 권장한다. 응답의 expiration은 SQLite에 저장한다.

historyId가 만료되어 `history.list`가 404를 반환하면 먼저 현재 mailbox historyId를 확보한다. 기존 검색 복구의 모든 페이지를 처리한 후 그 ID로 cursor를 교체한다. 복구 중 도착한 메일은 다음 history 읽기에 남는다. 1시간 복구 실행은 누락된 Pub/Sub 알림이 있어도 cursor부터 history를 다시 읽는다. 기존 Codex 1시간 검색은 전환 중 별도 안전망이며 새 경로 검증 전에는 중지하지 않는다. Pub/Sub 재전송은 이미 전달된 미확인 알림을 복구하지만 Gmail 자체 알림 누락까지 대신하지 못한다.

## 로컬 실행 계약

`push.example.json`을 참고해 개인 전용 비공개 설정을 만든다. `gmail_email`은 읽을 개인 mailbox, `topic_name`은 Gmail watch 게시 대상, `subscription_name`은 이 프로세스가 읽을 Pub/Sub 구독이다. 기존 `receiver.json`과 `gmail.sqlite`는 현재 개인 봇의 **동일한 영속 경로**를 사용해야 한다. Gmail OAuth는 개인 로그인 Keychain의 `personal-crashlytics-bot-oauth-user` 항목에서 읽는다. Pub/Sub 수신은 별도의 `personal-crashlytics-bot-pubsub-user` 항목과 `https://www.googleapis.com/auth/pubsub` 단일 범위를 요구한다. 후자를 Keychain에 저장했으며 별도 프로세스 조회는 미검증이다. 사용자가 일반 Terminal에서 별도 검증 스크립트의 `gmail_keychain_and_profile_ok`를 확인했다. 자동 서비스 프로세스의 Keychain 접근은 아직 검증되지 않았다. 자격증명은 Git에 넣지 않는다.

```bash
python3 -m pip install -r requirements-push.txt
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --renew-watch
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --serve
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --recover
```

`--serve`는 Pub/Sub StreamingPull을 사용하며 주기적 Gmail 메시지 조회가 아니다. `--renew-watch`는 매일, `--recover`는 누락 감시를 위해 한 시간마다 실행하도록 **승인 후** 운영 예약을 구성한다. 이 코드는 Cloud 프로젝트·topic·subscription·IAM·OAuth 자격증명이나 예약을 생성하지 않는다.

의존성 설치 명령은 서비스를 시작하지 않는다. 반면 `--renew-watch`는 즉시 Gmail watch를 등록하고, `--serve`는 즉시 구독을 시작하며 메시지에 따라 개인 앱 GitHub 이슈를 쓸 수 있다. `--recover`도 즉시 Gmail을 읽고 이슈를 쓸 수 있다. `launchctl bootstrap` 또는 `kickstart`로 만든 상주 작업도 즉시 실행될 수 있으므로 운영 Mac 경로와 권한 확인 전에는 실행하지 않는다.

직접 API의 최소 Gmail OAuth 범위는 `https://www.googleapis.com/auth/gmail.readonly`다. 전체 mailbox의 history와 `From` 메타데이터를 읽으며 Firebase 발신 후보에만 본문을 조회한다. 개인 OAuth 앱의 Google Cloud 프로젝트는 Gmail watch topic의 프로젝트와 일치해야 한다. 해당 topic에는 Gmail 게시 주체의 Pub/Sub Publisher 권한, 구독 실행 계정에는 해당 subscription의 Pub/Sub Subscriber 권한이 필요하다. 개인 Gmail connector의 OAuth나 회사용 자격증명을 이 프로세스에 복사하지 않는다. Keychain 로더는 요구 범위와 refresh token을 확인하고 다른 범위·누락·읽기 실패에서 수신기를 시작하지 않는다.

활성화 전에 개인 Gmail 계정과 읽기 범위, 개인 Cloud 프로젝트·topic·subscription, Gmail 게시 권한, Pub/Sub 구독자 인증, 운영 Mac과 공유 상태 경로, 개인 앱 GitHub 이슈 쓰기 대상에 대한 승인이 필요하다. 실제 `watch` 등록과 첫 메일 수신·ACK/NACK·중복·history 404·갱신·복구의 운영 검증 후 예약 전환을 결정한다. 회사 봇의 상태·인증·runner와 공유하지 않는다.

메시지 ID 선점과 기한 있는 lease는 공유 SQLite에 남는다. 정상 실패는 즉시 재시도 가능하고, 프로세스 중단 후에는 lease 만료 시 다시 선점한다. lease가 남아 있으면 체크포인트를 거부한다. 서로 다른 Gmail ID가 같은 크래시를 알리더라도 Mac의 외부 쓰기 lock이 이슈 조회·생성을 직렬화한다. 이슈 생성 POST 직전에 marker의 영속 intent를 기록한다. 응답 유실이나 중단 뒤에는 원격 이슈를 다시 조회한다. 일치하는 이슈가 보이지 않는 불확실한 POST는 **두 번째 생성 없이 보류**하고 사람이 원격 상태를 확인해야 한다. `last_scan_epoch`는 **전체 성공**을 뜻하며 실패 ID 목록과 별도다. 조회 검색식은 마지막 성공 시점보다 24시간 앞까지 겹친다. Mac이 오프라인이면 성공 체크포인트를 진행하지 않고, 복귀 후 마지막 성공 시점과 pending ID부터 복구한다.

`STATE_DIR`은 운영 호스트에서 **checkout 밖의 한 영속 디렉터리**로 정한다. 기존 개인 봇이 이미 비공개 설정·DB를 사용 중이면 그 위치와 내용을 먼저 확인하고 이어 쓴다. 회사 봇의 설정·DB·인증은 공유하지 않는다. 이벤트와 시간별 폴백은 checkout이 달라도 동일한 `receiver.json`, `gmail.sqlite`, `gmail.write.lock`을 사용한다. CLI의 단일 Mac 작업 lock은 `tempfile.gettempdir()/personal-crashlytics-bot.lock`에 있다. 둘 이상의 Mac에 복제해 동시에 실행하려면 공유 큐와 상태 저장소가 먼저 필요하다. 상태 파일과 임시 메일은 Git에서 제외한다.

## 활성화 전 확인

- 회사 Mac에서 개인 봇의 실제 checkout과 비공개 상태 경로를 확인한다. 다른 기기의 빈 상태를 운영 checkpoint로 대체하지 않는다.
- 회사 Mac에서 기존 개인 1시간 예약 작업의 실행 상태와 개인 봇의 마지막 성공 checkpoint를 확인한다. 빈 상태 디렉터리로 운영 상태를 초기화하지 않는다.
- 회사 Mac의 개인 `easyhooon` GitHub 인증과 대상 앱 저장소 Issues write, 개인 Gmail 연결의 profile ID를 확인한다. 회사 봇의 계정·인증·DB는 사용하지 않는다.
- 부모→운영 호스트의 개인 봇 작업 전달과 Gmail `full` 조회, 합성 메일 처리, 재전달, 폴백, 실패 복구를 검증한다. `main`의 테스트는 합성 입력과 가짜 GitHub 클라이언트만 사용했다.
- 기존 1시간 작업과 새 흐름이 서로 다른 체크포인트 또는 Mac lock을 쓰면 동시에 이슈를 생성할 수 있다. 합성 검증 후 단일 writer와 체크포인트를 확정하고, 기존 작업과 새 쓰기 흐름이 겹치지 않게 전환한다. 새 흐름을 검증하기 전에는 기존 작업을 중지하지 않는다.

이 문서는 기존 수신기 계약과 전환 계획이다. 문서 수정만으로 Gmail 이벤트나 예약 작업은 활성화되지 않는다. 직접 API 방식의 설정·검증 절차는 [개인 Gmail 설정 가이드](docs/PERSONAL_GMAIL_SETUP.md)를 따른다.

참고: [Gmail push 가이드](https://developers.google.com/workspace/gmail/api/guides/push), [Gmail history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list).
