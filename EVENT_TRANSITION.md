# Gmail 메시지 이벤트와 시간별 복구 조회

현재 수신기는 Gmail 메시지의 발신자 인증과 정확한 Firebase issue URL을 검사해 두 Android 앱만 이슈 처리한다. 원문, stack, 메일의 명령은 GitHub로 보내지 않는다. GitHub 이슈 marker와 Gmail ID 기록으로 재전달을 중복 처리하지 않는다.

## 실행 경로와 입력 계약

Gmail 메시지 수신 이벤트는 부모 작업을 깨운다. 운영 호스트 접근이 확인되면 부모는 그 호스트의 개인 봇 작업에 **Gmail 메시지 ID만** 전달한다. 전달 형식은 `{"mode":"event","message_ids":["<Gmail hex ID>"]}`이다. 발신 주소만 트리거 필터로 쓰고 제목에는 Crashlytics 단어가 있을 것을 요구하지 않는다. 같은 발신자의 billing 등 다른 메일은 기존 `normalize()`의 앱·project·issue URL 검사에서 제외한다. 부모와 자식 작업 사이에 메일 본문, stack, 개인정보, GitHub 토큰을 메시지로 전달하지 않는다.

운영 작업은 비공개 런타임 설정의 Gmail 연결 ID와 profile ID를 실제 개인 Gmail 연결과 비교한다. **Gmail 메시지를 읽기 전에** 받은 ID를 아래 명령으로 영속 pending에 기록한다. 조회 자체가 실패해도 시간별 폴백이 이 ID를 다시 읽을 수 있다. 이벤트 ID마다 같은 개인 Gmail 연결에서 `full` 메시지를 다시 읽는다. 이 객체를 권한 0600의 일회성 JSON 파일에 저장하고 수신 명령을 실행한 뒤 성공·실패 모두 파일을 삭제한다. 모든 명령에는 동일한 공유 상태 파일 경로를 명시한다.

```bash
python3 -B mail_receiver.py --runtime-config STATE_DIR/receiver.json --state STATE_DIR/gmail.sqlite --enqueue-id GMAIL_MESSAGE_ID
python3 -B mail_receiver.py --runtime-config STATE_DIR/receiver.json --state STATE_DIR/gmail.sqlite --gmail-file /private/tmp/gmail-message.json
```

시간별 폴백은 별도 예약 작업이 같은 운영 호스트의 개인 봇 작업에 `{"mode":"scan"}`을 전달한다. 운영 작업은 다음 명령으로 검색식, 연결·profile ID, 성공 조회의 시작 시각, 실패 또는 중단된 Gmail ID(`pending_ids`)를 얻는다.

```bash
python3 -B mail_receiver.py --runtime-config STATE_DIR/receiver.json --state STATE_DIR/gmail.sqlite --poll-query
```

작업은 먼저 `pending_ids`의 `full` 메시지를 다시 읽어 처리한다. 이어서 반환된 검색식의 **모든 페이지**를 읽고, 각 검색 결과 ID를 `--enqueue-id`로 기록한 뒤 해당 메시지의 `full` 객체를 처리한다. 같은 ID는 SQLite가 재처리를 막는다. 페이지 조회 또는 메시지 처리 하나라도 실패하거나 현재 처리 중이면 체크포인트를 진행하지 않는다. 전체 성공 후에만 처음 받은 `scan_started_epoch`로 갱신한다.

```bash
python3 -B mail_receiver.py --runtime-config STATE_DIR/receiver.json --state STATE_DIR/gmail.sqlite --checkpoint SCAN_STARTED_EPOCH
```

메시지 ID 선점과 기한 있는 lease는 공유 SQLite에 남는다. 정상 실패는 즉시 재시도 가능하고, 프로세스 중단 후에는 lease 만료 시 다시 선점한다. lease가 남아 있으면 체크포인트를 거부한다. 서로 다른 Gmail ID가 같은 크래시를 알리더라도 Mac의 외부 쓰기 lock이 이슈 조회·생성을 직렬화한다. 이슈 생성 POST 직전에 marker의 영속 intent를 기록한다. 응답 유실이나 중단 뒤에는 원격 이슈를 다시 조회한다. 일치하는 이슈가 보이지 않는 불확실한 POST는 **두 번째 생성 없이 보류**하고 사람이 원격 상태를 확인해야 한다. `last_scan_epoch`는 **전체 성공**을 뜻하며 실패 ID 목록과 별도다. 조회 검색식은 마지막 성공 시점보다 24시간 앞까지 겹친다. Mac이 오프라인이면 성공 체크포인트를 진행하지 않고, 복귀 후 마지막 성공 시점과 pending ID부터 복구한다.

`STATE_DIR`은 운영 호스트에서 **checkout 밖의 한 영속 디렉터리**로 정한다. 기존 개인 봇이 이미 비공개 설정·DB를 사용 중이면 그 위치와 내용을 먼저 확인하고 이어 쓴다. 회사 봇의 설정·DB·인증은 공유하지 않는다. 이벤트와 시간별 폴백은 checkout이 달라도 동일한 `receiver.json`, `gmail.sqlite`, `gmail.write.lock`을 사용한다. CLI의 단일 Mac 작업 lock은 `tempfile.gettempdir()/personal-crashlytics-bot.lock`에 있다. 둘 이상의 Mac에 복제해 동시에 실행하려면 공유 큐와 상태 저장소가 먼저 필요하다. 상태 파일과 임시 메일은 Git에서 제외한다.

## 활성화 전 확인

- 사용자가 운영 호스트로 지정한 회사 Mac의 접근 권한을 확인한다. 현재 집 Mac은 개발·합성 검증 전용이다. 회사 Mac의 실제 checkout 및 개인 봇 상태 경로는 확인 전까지 추정하지 않는다.
- 회사 Mac에서 기존 개인 15분 예약 작업의 존재·위치·소유 계정·실행 상태와 개인 봇의 마지막 성공 checkpoint를 확인한다. 집 Mac의 빈 상태 디렉터리로 운영 상태를 초기화하지 않는다.
- 회사 Mac의 개인 `easyhooon` GitHub 인증과 대상 앱 저장소 Issues write, 개인 Gmail 연결의 profile ID를 확인한다. 회사 봇의 계정·인증·DB는 사용하지 않는다.
- 부모→운영 호스트의 개인 봇 작업 전달과 Gmail `full` 조회, 합성 메일 처리, 재전달, 폴백, 실패 복구를 검증한다. 이 브랜치의 테스트는 합성 입력과 가짜 GitHub 클라이언트만 사용했다.
- 기존 15분 작업과 새 흐름이 서로 다른 체크포인트 또는 Mac lock을 쓰면 동시에 이슈를 생성할 수 있다. 합성 검증 후 단일 writer와 체크포인트를 확정하고, 기존 작업과 새 쓰기 흐름이 겹치지 않게 전환한다. 기존 작업을 찾거나 새 흐름을 검증하기 전에는 중지하지 않는다.

이 문서는 실행 계약이다. Gmail 이벤트, 예약 작업, GitHub 이슈 생성 운영을 활성화하지 않는다. 새 Gmail watch/PubSub, 서비스 계정, OAuth 설정은 필요하다고 확인되지 않았다.
