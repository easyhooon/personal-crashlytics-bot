# 개인 Gmail 이벤트 수신 설정

이 가이드는 개인 Gmail `watch` → Cloud Pub/Sub → StreamingPull → `history.list` 경로를 재현하는 순서다. `PERSONAL_*`은 각자 확인한 값으로 바꾼다. 실제 계정, 프로젝트, 앱 ID, 토큰, 비공개 경로를 저장소에 기록하지 않는다. 회사 계정·프로젝트·인증·상태 DB와 혼합하지 않는다.

**2026-10-12 확인 범위:** 개인 Cloud 콘솔에서 Gmail API와 Pub/Sub API, 외부 Testing OAuth 앱의 테스트 사용자·Desktop 클라이언트·`gmail.readonly` 범위, Pub/Sub 주제·pull 구독(7일 보관)·Gmail 게시자 권한을 확인했다. OAuth 사용자 토큰, 로컬 ADC, Gmail watch, 상주 수신기, 실제 알림 수신·ACK/NACK, 갱신 예약은 **미검증**이다. 프로젝트에 결제 계정이 연결되지 않았으며 결제가 요구되면 연결 전에 별도로 판단한다. 기존 1시간 복구 조회는 새 경로 검증 전까지 유지한다.

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

기능 브랜치의 `gmail_push.py`는 `PERSONAL_GMAIL_OAUTH_FILE`이 가리키는 **authorized-user JSON**을 읽는다. Desktop 클라이언트 JSON에는 사용자 refresh token이 없으므로 그것만으로 실행되지 않는다. OAuth 동의·토큰 발급은 별도로 완료해야 한다. Keychain을 택한다면 본인 계정에서 사용자 인증 후 토큰을 안전하게 저장하고, **Keychain 로딩 코드가 준비되었는지** 확인한다. 현재 코드는 Keychain을 직접 읽지 않는다. 이 작업에서 Keychain 쓰기는 OS 승인 거부로 끝났으므로 저장됐다고 가정하지 않는다. 파일 방식이라면 checkout 밖 영속 경로, 소유자 전용 권한, 백업·폐기 절차를 적용한다.

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

실제 수신 프로세스의 실행 주체에는 해당 구독의 `roles/pubsub.subscriber`가 필요하다. 이미 상속받은 권한이 있다면 중복 부여하지 않는다. 새 서비스 계정 키 파일보다 승인된 ADC 등 안전한 기존 인증을 사용한다. 주제·구독은 Gmail watch에 사용하는 **동일한 Cloud 프로젝트**에 둔다.

## 4. 비공개 설정과 첫 수신

직접 수신 코드는 [`feat/event-receiver-completion`](https://github.com/easyhooon/personal-crashlytics-bot/tree/feat/event-receiver-completion)에 있고 이 문서 작성 시 `main`에 병합되지 않았다. 해당 브랜치의 `push.example.json`을 참고해 개인 Gmail 주소와 완전한 `projects/PERSONAL_PROJECT_ID/topics/PERSONAL_TOPIC`, `projects/PERSONAL_PROJECT_ID/subscriptions/PERSONAL_SUBSCRIPTION`을 적는다. 기존 복구 작업의 `receiver.json`과 `gmail.sqlite`는 운영 호스트의 **같은 영속 경로**를 사용한다. 다른 Mac이나 빈 checkout의 DB로 기존 checkpoint를 대체하지 않는다.

```bash
python3 -m pip install -r requirements-push.txt
export PERSONAL_GMAIL_OAUTH_FILE=/private/path/to/authorized-user.json
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --renew-watch
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --serve
```

`--renew-watch`는 즉시 Gmail 알림을 등록한다. `--serve`는 즉시 구독을 읽고 개인 앱 GitHub 이슈를 쓸 수 있다. 먼저 개인 계정·상태 경로·대상 저장소 Issues write를 확인한다. 첫 watch 응답의 historyId와 expiration이 저장되고 기존 겹침 검색 뒤 cursor가 설정되는지 관찰한다. watch 갱신은 매일 예약할 수 있으며 이는 **구독 유지 호출**이지 메시지 폴링이 아니다. 갱신은 처리 cursor를 덮어쓰지 않아야 한다. 운영 예약 활성화는 실제 경로 검증 후 진행한다.

## 5. 중복·실패·복구 검증

`python3 -B -m unittest -q`로 로컬 테스트를 실행한다. 이후 허용된 시험 알림으로 Pub/Sub delivery → Gmail history 전체 페이지 → pending 저장 → 대상 분류 → ACK를 관찰한다. 같은 알림 재전송과 동시 callback에서 이슈 중복이 없는지, 실패 때 NACK 후 재전달되는지, ACK 직전 중단 후 재개되는지 확인한다. 비대상 메일은 `From` 메타데이터만 읽고 이슈를 쓰지 않아야 한다. **실제 수신과 이슈 쓰기는 이 작업에서 미검증**이다.

누락 의심 시 `--recover`는 저장된 history cursor에서 다시 읽는다. Gmail historyId 만료로 404가 나오면 현재 mailbox historyId를 먼저 확보하고, 검색의 **모든 페이지**와 pending ID를 처리한 후 cursor를 바꾼다. 복구 중 도착한 메일은 이후 history에서 다시 처리한다. 불확실한 GitHub POST는 두 번째 이슈를 자동 생성하지 않고 사람이 원격 상태를 확인한다. Pub/Sub 재전송은 미확인 알림을 다시 보내지만 Gmail 알림 자체의 누락을 모두 보장하지 못한다. 기존 1시간 복구 조회는 새 경로와 공유 상태가 운영에서 검증될 때까지 유지한다.

```bash
python3 -B gmail_push.py --config PRIVATE_PUSH_JSON \
  --runtime-config PRIVATE_RECEIVER_JSON --state PRIVATE_GMAIL_SQLITE --recover
```

가동 상태는 로컬 `launchd`, Codex 작업, GitHub Actions, Cloud Scheduler를 각각 읽어 확인한다. 저장소에 코드나 설정 예시가 있다는 이유만으로 배포됐다고 판단하지 않는다. 연결 후 실제 delivery, cursor, ACK, checkpoint 관찰 결과를 기록한다.
