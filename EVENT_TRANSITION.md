# 개인 Gmail push 수신 전환

현재 운영 중인 것은 기존 Codex 1시간 Gmail 검색 복구 작업이다. 이 문서의 직접 Gmail API `watch` → Cloud Pub/Sub StreamingPull → `history.list` 경로는 **코드와 fixture 테스트만 준비**됐다. 개인 OAuth, Pub/Sub 자원, watch, 상주 서비스, 갱신·복구 예약은 설정하거나 활성화하지 않았다. 기존 복구 작업은 새 경로의 실제 수신을 확인할 때까지 유지한다.

## 입력과 처리

Pub/Sub 데이터는 Gmail 메시지 ID가 아닌 `{"emailAddress":"…","historyId":"…"}`다. 수신기는 설정된 개인 계정과 일치하는지 검사하고, SQLite에 저장한 직전 cursor부터 `history.list(historyTypes=messageAdded)`의 모든 페이지를 읽는다. 각 메시지 ID를 기존 SQLite pending에 저장한 다음 `From` 메타데이터만 조회한다. Firebase 발신 메일에 한해 Gmail `full` 객체를 읽고 기존 수신기의 DKIM/DMARC 인증과 두 production Android 앱의 정확한 이슈 URL을 검증해 GitHub 이슈를 동기화한다. 일반 메일은 본문을 읽지 않고 처리 ID만 기록한다. 메일 원문과 stack은 GitHub 이슈에 올리지 않는다.

모든 pending 재처리와 history 페이지가 끝나야 cursor를 전진시킨다. 그 뒤 Pub/Sub를 ACK한다. Gmail·GitHub·상태 저장 중 실패하면 NACK하며 cursor를 유지한다. 재전송과 동시 callback은 같은 Mac의 작업 lock, SQLite 메시지 선점, 원격 이슈 marker로 중복 쓰기를 막는다. 프로세스가 쓰기 후 ACK 전에 종료돼도 다음 delivery가 저장된 cursor 또는 pending에서 재개한다. 이 보장은 한 Mac과 한 공유 상태 경로에 한정된다.

초기 `watch`는 현재 historyId를 받은 뒤 기존 겹침 검색을 완료하고 cursor를 저장한다. 따라서 검색 중 도착한 메일은 이후 history에서 다시 보인다. 이후 `watch` 갱신은 cursor를 덮어쓰지 않는다. 갱신은 메시지를 주기적으로 읽는 폴링이 아니라 구독 유지 호출이다. Gmail은 최소 7일마다 갱신을 요구하고 매일 갱신을 권장한다. 응답의 expiration은 SQLite에 저장한다.

historyId가 만료되어 `history.list`가 404를 반환하면 먼저 현재 mailbox historyId를 확보한다. 기존 검색 복구의 모든 페이지를 처리한 후 그 ID로 cursor를 교체한다. 복구 중 도착한 메일은 다음 history 읽기에 남는다. 1시간 복구 실행은 누락된 Pub/Sub 알림이 있어도 cursor부터 history를 다시 읽는다. 기존 Codex 1시간 검색은 전환 중 별도 안전망이며 새 경로 검증 전에는 중지하지 않는다. Pub/Sub 재전송은 이미 전달된 미확인 알림을 복구하지만 Gmail 자체 알림 누락까지 대신하지 못한다.

## 로컬 실행 계약

`push.example.json`을 참고해 개인 전용 비공개 설정을 만든다. `gmail_email`은 읽을 개인 mailbox, `topic_name`은 Gmail watch 게시 대상, `subscription_name`은 이 프로세스가 읽을 Pub/Sub 구독이다. 기존 `receiver.json`과 `gmail.sqlite`는 현재 개인 봇의 **동일한 영속 경로**를 사용해야 한다. `PERSONAL_GMAIL_OAUTH_FILE`은 승인된 기존 사용자 OAuth authorized-user 파일 경로다. Pub/Sub 구독 인증은 승인된 Google ADC를 사용한다. 자격증명은 Git에 넣지 않는다.

```bash
python3 -m pip install -r requirements-push.txt
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --renew-watch
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --serve
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --recover
```

`--serve`는 Pub/Sub StreamingPull을 사용하며 주기적 Gmail 메시지 조회가 아니다. `--renew-watch`는 매일, `--recover`는 누락 감시를 위해 한 시간마다 실행하도록 **승인 후** 운영 예약을 구성한다. 이 코드는 Cloud 프로젝트·topic·subscription·IAM·OAuth 자격증명이나 예약을 생성하지 않는다. 현재 기존 Gmail connector 인증이 별도 Python OAuth 파일로 재사용 가능한지는 확인되지 않았다.

의존성 설치 명령은 서비스를 시작하지 않는다. 반면 `--renew-watch`는 즉시 Gmail watch를 등록하고, `--serve`는 즉시 구독을 시작하며 메시지에 따라 개인 앱 GitHub 이슈를 쓸 수 있다. `--recover`도 즉시 Gmail을 읽고 이슈를 쓸 수 있다. `launchctl bootstrap` 또는 `kickstart`로 만든 상주 작업도 즉시 실행될 수 있으므로 운영 Mac 경로와 권한 확인 전에는 실행하지 않는다.

직접 API의 최소 Gmail OAuth 범위는 `https://www.googleapis.com/auth/gmail.readonly`다. 전체 mailbox의 history와 `From` 메타데이터를 읽으며 Firebase 발신 후보에만 본문을 조회한다. 개인 OAuth 앱의 Google Cloud 프로젝트는 Gmail watch topic의 프로젝트와 일치해야 한다. 해당 topic에는 Gmail 게시 주체의 Pub/Sub Publisher 권한, 구독 실행 계정에는 해당 subscription의 Pub/Sub Subscriber 권한이 필요하다. 개인 Gmail connector의 OAuth나 회사용 자격증명을 이 프로세스에 복사하지 않는다. OAuth 파일 대신 Mac Keychain을 사용하려면 별도의 승인된 credential 로딩 경로가 필요하며 현재 코드는 이를 구현하지 않았다.

활성화 전에 개인 Gmail 계정과 읽기 범위, 개인 Cloud 프로젝트·topic·subscription, Gmail 게시 권한, Pub/Sub 구독자 인증, 운영 Mac과 공유 상태 경로, 개인 앱 GitHub 이슈 쓰기 대상에 대한 승인이 필요하다. 실제 `watch` 등록과 첫 메일 수신·ACK/NACK·중복·history 404·갱신·복구의 운영 검증 후 예약 전환을 결정한다. 회사 봇의 상태·인증·runner와 공유하지 않는다.

참고: [Gmail push 가이드](https://developers.google.com/workspace/gmail/api/guides/push), [Gmail history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list).
