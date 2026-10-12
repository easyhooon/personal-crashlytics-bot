# 개인 Gmail 이벤트 수신 설정

이 가이드는 개인 Gmail `watch` → Cloud Pub/Sub → StreamingPull → `history.list` 경로를 재현하는 순서다. `PERSONAL_*`은 각자 확인한 값으로 바꾼다. 실제 계정, 프로젝트, 앱 ID, 토큰, 비공개 경로를 저장소에 기록하지 않는다. 회사 계정·프로젝트·인증·상태 DB와 혼합하지 않는다.

**2026-10-12 확인 범위:** 개인 Cloud 콘솔에서 Gmail API와 Pub/Sub API, 외부 Testing OAuth 앱의 테스트 사용자·Desktop 클라이언트·`gmail.readonly` 범위, Pub/Sub 주제·pull 구독(7일 보관)·Gmail 게시자 권한을 확인했다. Gmail·Pub/Sub 각각의 단일 범위 OAuth를 Keychain에 저장하고 별도 프로세스의 Gmail `getProfile`·Pub/Sub 구독 조회를 확인했다. 운영 Mac의 LaunchAgent 서비스가 실행 중이며 Gmail watch를 등록하고 초기 Pub/Sub 알림의 history 조회·cursor 저장·ACK를 확인했다. 실제 신규 Firebase 알림 처리와 watch 갱신 예약은 **미검증**이다. 프로젝트에 결제 계정이 연결되지 않았으며 결제가 요구되면 연결 전에 별도로 판단한다. 기존 1시간 복구 조회는 새 경로 검증 전까지 유지한다.

## Gmail watch가 동작하는 방식

1. 이 Mac의 `gmail_push.py --renew-watch`가 개인 Gmail API `users.watch`를 호출해 프로젝트의 Pub/Sub topic을 등록한다. 이후 **Gmail 서버가** mailbox 변경을 그 topic에 게시한다. 등록 직후에도 초기 알림 한 건을 보낸다. [Gmail push 가이드](https://developers.google.com/workspace/gmail/api/guides/push)
2. Pub/Sub 알림에는 메일 본문이나 Gmail 메시지 ID가 아닌 `emailAddress`와 mailbox `historyId`가 들어 있다. 공식 예시는 `historyId`를 문자열로 표시하지만 이번 초기 알림은 JSON 정수였고, 수신기는 둘 다 검증해 처리한다. 이 Mac의 `--serve`는 [StreamingPull 연결](https://cloud.google.com/pubsub/docs/pull)을 열어 알림을 받는다. 수신기는 저장된 cursor부터 `history.list`의 모든 페이지를 조회한 뒤, Firebase 발신 후보만 메일 내용을 읽고 두 개인 앱의 이슈를 처리한다. 메일 전체를 일정 간격으로 조회하는 15분 폴링은 이 경로에 없다.
3. pending 처리와 history 조회가 성공하고 cursor가 SQLite에 저장된 뒤에만 Pub/Sub ACK를 보낸다. 실패 시 NACK해 재전달받고, 같은 Gmail ID는 SQLite와 이슈 marker로 중복 처리를 막는다. `historyId`가 만료되면 겹침 검색으로 복구한다. Gmail 자체 알림은 드물게 지연·누락될 수 있으므로 현재 1시간 검색 복구 작업은 계속 실행한다. [Gmail 신뢰성 안내](https://developers.google.com/workspace/gmail/api/guides/push#limitations)

Gmail watch는 **최소 7일 이내 재등록**해야 하며 Google은 하루 한 번 갱신을 권장한다. 이는 구독 등록을 유지하는 호출이고 메일 수신 폴링이 아니다. 현재 첫 watch의 만료 시각은 비공개 SQLite에 저장됐고, 일일 갱신 예약은 아직 활성화하지 않았다. 별개로 외부 **Testing** OAuth 앱의 refresh token은 Google 정책상 7일 후 만료될 수 있다. watch를 갱신해도 OAuth 만료가 연장되지는 않는다. [Gmail watch 갱신](https://developers.google.com/workspace/gmail/api/guides/push#renew_mailbox_watch), [OAuth refresh token 만료](https://developers.google.com/identity/protocols/oauth2#expiration)

현재 수신기는 사용자 로그인 세션의 Mac LaunchAgent다. 로그인 세션에서 서비스 PID와 초기 ACK를 확인했지만 Mac 절전·네트워크 단절·재부팅 뒤 자동 재개는 아직 실증하지 않았다. Mac이 깨어 있고 네트워크가 연결되어야 StreamingPull이 즉시 처리한다. 중단 동안의 알림은 Pub/Sub 구독 보관 기간 안에 다시 받을 수 있으며, 그 밖의 누락은 cursor와 기존 시간별 복구 경로로 확인한다. 신규 Firebase 메일이 실제로 도착해 GitHub 이슈까지 처리되는 과정도 아직 검증되지 않았다.

## 1. 계정과 프로젝트

Google Cloud CLI가 없다면 [공식 설치 안내](https://cloud.google.com/sdk/docs/install)를 따른다. CLI 기본값이 회사 프로필일 수 있으므로 읽기 명령으로 확인한다.

```bash
gcloud version
gcloud auth list
gcloud config list account project
gcloud projects describe PERSONAL_PROJECT_ID
```

실행 계정·프로젝트가 개인용인지 재확인한다. `gcloud auth application-default login`은 별도의 로컬 ADC이며 Cloud 콘솔 로그인이나 Gmail OAuth 사용자 토큰을 대신하지 않는다. 이 작업 환경에서는 CLI 자격증명 저장소의 로컬 보안 접근이 차단되어 인증된 `gcloud` 명령을 실행·검증하지 못했다. 아래 CLI 절차는 **미검증**이다.

## 2. API와 OAuth

CLI에서 API를 사용 설정하고 목록을 확인할 수 있다.

```bash
gcloud services enable gmail.googleapis.com pubsub.googleapis.com --project=PERSONAL_PROJECT_ID
gcloud services list --enabled --project=PERSONAL_PROJECT_ID
```

[Google 인증 플랫폼 콘솔](https://console.cloud.google.com/auth/overview)에서 화면마다 개인 프로젝트 표시를 확인한다.

1. **브랜딩**에 개인 앱 이름과 본인 연락처를 입력한다. **대상**은 외부, 상태는 **Testing**으로 둔다.
2. **대상**에 개인 Gmail 주소를 테스트 사용자로 등록한다. 회사 주소를 넣지 않는다.
3. **클라이언트**에서 **데스크톱 앱** OAuth 클라이언트를 만든다. 발급 JSON에는 비밀값이 있으므로 Git 밖에 소유자만 읽을 수 있게 보관한다.
4. **데이터 액세스**에 `https://www.googleapis.com/auth/gmail.readonly`만 추가하고 저장한다. 이 수신기에는 Gmail 수정·전체 Cloud 권한이 필요하지 않다.

외부 Testing 앱의 제한 범위 refresh token에는 [7일 만료 제한](https://developers.google.com/identity/protocols/oauth2#expiration)이 적용될 수 있다. 장기 운영 전 재인증 절차와 Production 전환을 **별도로** 판단한다. Production 공개나 추가 범위는 이 가이드의 자동 단계가 아니다.

`gmail_push.py`는 개인 로그인 Keychain의 `personal-crashlytics-bot-oauth-user`에서 `gmail.readonly` 사용자 자격증명을 읽고 refresh token과 단일 범위를 확인한다. Desktop 클라이언트 JSON은 별도의 `personal-crashlytics-bot-oauth-client` Keychain 항목에 저장됐지만 런타임 수신기에서 직접 읽지 않는다. 사용자의 일반 macOS Terminal과 운영 checkout의 별도 프로세스에서 `scripts/verify_gmail_keychain.py`의 `gmail_keychain_and_profile_ok`를 확인했다. LaunchAgent도 초기 알림을 ACK했으며 재부팅 뒤 Keychain 접근은 별도로 확인해야 한다. 파일과 Keychain 값은 Git에 넣지 않는다. 이 외부 Testing 앱의 refresh token 만료에 대비해 재인증 절차를 마련한다.

Pub/Sub 수신은 `personal-crashlytics-bot-pubsub-user` Keychain 항목에 `https://www.googleapis.com/auth/pubsub` **단일 범위**의 별도 사용자 OAuth를 요구한다. 사용자가 동의하고 `scripts/authorize_pubsub.py`의 `pubsub_oauth_keychain_saved`를 확인했다. 운영 checkout의 별도 프로세스에서 `scripts/verify_pubsub_keychain.py --subscription projects/PERSONAL_PROJECT/subscriptions/PERSONAL_SUBSCRIPTION`의 `pubsub_scope_and_subscription_ok`도 확인했다. 이 검증 자체는 pull·ACK를 실행하지 않는다. Google의 [Pull](https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/pull)과 [ACK](https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/acknowledge) 문서는 이 범위를 허용한다. 기본 `gcloud auth application-default login`의 넓은 Cloud 범위를 이 절차에 사용하지 않는다. Keychain 읽기 실패·범위 불일치·refresh token 누락 시 수신기는 시작하지 않는다.

## 3. Pub/Sub와 최소 IAM

[Pub/Sub 콘솔](https://console.cloud.google.com/cloudpubsub/topic/list)에서 개인 프로젝트를 확인한다. 주제를 만들 때 **기본 구독 추가**를 끈다. 주제에 `gmail-api-push@system.gserviceaccount.com`을 **Pub/Sub 게시자**로 추가한다. 프로젝트 전체가 아닌 주제 권한이다. 해당 주제에 **가져오기(pull)** 구독을 하나 만들고 메시지를 7일 보관한다. 이 pull 구독을 사용하는 `--serve`는 StreamingPull 장기 연결이며 15분 간격 Gmail 조회가 아니다.

동일 구성의 CLI 예시이며, 이 환경에서는 실행을 검증하지 못했다.

```bash
gcloud pubsub topics create PERSONAL_TOPIC --project=PERSONAL_PROJECT_ID
gcloud pubsub topics add-iam-policy-binding PERSONAL_TOPIC \
  --project=PERSONAL_PROJECT_ID \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher
gcloud pubsub subscriptions create PERSONAL_SUBSCRIPTION \
  --topic=PERSONAL_TOPIC --message-retention-duration=7d \
  --project=PERSONAL_PROJECT_ID
gcloud pubsub topics get-iam-policy PERSONAL_TOPIC --project=PERSONAL_PROJECT_ID
gcloud pubsub subscriptions describe PERSONAL_SUBSCRIPTION --project=PERSONAL_PROJECT_ID
```

실제 수신 프로세스의 실행 주체에는 해당 구독의 `roles/pubsub.subscriber`가 필요하다. 이미 상속받은 권한이 있다면 중복 부여하지 않는다. 새 서비스 계정 키 파일을 만들지 않고 위의 개인 Pub/Sub 범위 OAuth를 사용한다. 주제·구독은 Gmail watch에 사용하는 **동일한 Cloud 프로젝트**에 둔다.

## 4. 비공개 설정과 첫 수신

직접 수신 코드 [`gmail_push.py`](../gmail_push.py)와 [`push.example.json`](../push.example.json)을 참고해 개인 Gmail 주소와 완전한 `projects/PERSONAL_PROJECT_ID/topics/PERSONAL_TOPIC`, `projects/PERSONAL_PROJECT_ID/subscriptions/PERSONAL_SUBSCRIPTION`을 적는다. 기존 복구 작업의 `receiver.json`과 `gmail.sqlite`는 운영 호스트의 **같은 영속 경로**를 사용한다. 다른 Mac이나 빈 checkout의 DB로 기존 checkpoint를 대체하지 않는다.

```bash
python3 -m pip install -r requirements-push.txt
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --renew-watch
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --serve
```

`--renew-watch`는 즉시 Gmail 알림을 등록한다. `--serve`는 즉시 구독을 읽고 개인 앱 GitHub 이슈를 쓸 수 있다. 먼저 개인 계정·상태 경로·대상 저장소 Issues write를 확인한다. 첫 watch 응답의 historyId와 expiration이 저장되고 기존 겹침 검색 뒤 cursor가 설정되는지 관찰한다. watch 갱신은 매일 예약할 수 있으며 이는 **구독 유지 호출**이지 메시지 폴링이 아니다. 갱신은 처리 cursor를 덮어쓰지 않아야 한다. 운영 예약 활성화는 실제 경로 검증 후 진행한다.

## 5. 중복·실패·복구 검증

`python3 -B -m unittest -q`로 로컬 테스트를 실행한다. 초기 watch 알림의 Pub/Sub delivery → Gmail history 조회 → cursor 저장 → ACK는 운영에서 확인했다. 재전달·동시 callback의 중복 방지, 실패 NACK, ACK 직전 중단, history 404 복구는 fixture 테스트로 확인했다. 신규 Firebase 메일이 도착했을 때 대상 분류와 이슈 쓰기까지 이어지는 **운영 검증은 아직 없다**. 비대상 메일은 `From` 메타데이터만 읽고 이슈를 쓰지 않아야 한다.

누락 의심 시 `--recover`는 저장된 history cursor에서 다시 읽는다. Gmail historyId 만료로 404가 나오면 현재 mailbox historyId를 먼저 확보하고, 검색의 **모든 페이지**와 pending ID를 처리한 후 cursor를 바꾼다. 복구 중 도착한 메일은 이후 history에서 다시 처리한다. 불확실한 GitHub POST는 두 번째 이슈를 자동 생성하지 않고 사람이 원격 상태를 확인한다. Pub/Sub 재전송은 미확인 알림을 다시 보내지만 Gmail 알림 자체의 누락을 모두 보장하지 못한다. 기존 1시간 복구 조회는 새 경로와 공유 상태가 운영에서 검증될 때까지 유지한다.

```bash
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --recover
```

가동 상태는 로컬 `launchd`, Codex 작업, GitHub Actions, Cloud Scheduler를 각각 읽어 확인한다. 저장소에 코드나 설정 예시가 있다는 이유만으로 배포됐다고 판단하지 않는다. 연결 후 실제 delivery, cursor, ACK, checkpoint 관찰 결과를 기록한다.
