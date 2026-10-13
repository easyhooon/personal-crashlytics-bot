# 크래시 수정 판단 경계

현재 운영 수신기는 인증된 Crashlytics 메일을 개인 GitHub 이슈로 기록한다. 선택적 자동 분석·수정·Draft PR 코드는 구현됐지만 운영 LaunchAgent와 1시간 복구 예약에는 `--repair-config`가 없어 활성화되지 않았다. `bot.py --probe`는 README를 바꾸는 합성 연결 검사이며 크래시 수정 경로가 아니다.

`crashlytics_mcp.py`는 Firebase CLI의 공식 MCP 서버에서 `crashlytics_get_issue`와 `crashlytics_list_events` 두 읽기 전용 도구만 호출한다. 입력 앱과 이슈 ID는 `bot.TARGETS`로 검증한다. 샘플 이벤트에는 로그와 breadcrumb가 포함될 수 있으므로 원문을 stdout, GitHub, 저장소 파일에 출력하지 않는다. MCP 인증 실패·이슈 불일치·샘플 누락은 분석을 중단한다.

`repair_triage.py`는 확인된 사실과 미확인 사항을 기존 이슈에 남긴다. `CrashedByAdbException`이 Firebase 이슈와 샘플 이벤트에 모두 있으면 셸 주입 테스트로 분류하고 앱 코드 수정 없이 종료한다. 그 밖의 원인은 증거에 따라 `needs-investigation` 또는 `verification-failed`로 남긴다. 분석 코멘트는 Firebase 이슈와 GitHub 이슈 marker가 일치할 때만 작성하며, 분류별 marker로 재실행 시 중복을 막는다.

```bash
python3 -B repair_triage.py --alert-file PRIVATE_NORMALIZED_ALERT.json --issue-number NUMBER --dry-run
python3 -B repair_triage.py --alert-file PRIVATE_NORMALIZED_ALERT.json --issue-number NUMBER
```

두 번째 명령은 이슈 코멘트를 실제로 작성한다. 운영 LaunchAgent에는 아직 등록하지 않았다.

`repair_policy.assess`는 수정 PR 후보의 최소 조건을 검사한다. Firebase 샘플 이벤트, 앱 소유 stack 프레임과 같은 소스 파일의 변경, 앱 코드가 원인인 구체적 설명, 수정 전 실패한 회귀 테스트, 수정 후 통과한 회귀 테스트와 실제 프로젝트 검사, 검사한 commit SHA가 모두 필요하다. 모델의 확신 점수나 자체 테스트 통과 문장은 조건이 아니다. 셸 주입, 외부 원인, 증거 부족, 관련 없는 변경, 검증 불가에서는 `issue_only`를 반환한다. 제조사 한정 보고 자체는 배제 조건이 아니며 같은 앱 코드 근거와 검증 조건을 적용한다.

`codex_repair.py`는 Codex CLI를 읽기 전용으로 실행하고, 허용 앱 ID와 이슈 ID만 전달해 Firebase MCP의 위 두 도구와 대상 소스를 읽힌다. 모델 결과는 구조화된 설명·회귀 테스트 패치·관련 Kotlin 소스 패치다. `repair_project.py`가 허용 파일·정확한 앱 stack 프레임·예외·소스 라인을 검사하고 별도로 테스트를 실행한다. 새 테스트가 수정 전 해당 예외로 실패하고 수정 후 통과해야 하며 실제 프로젝트 검사도 통과해야 한다. 생성된 테스트 실행은 네트워크를 차단한 `codex sandbox -P repair-tests`를 요구한다. 이 프로필은 현재 구성·실행 검증이 끝나지 않았으므로 실제 수정을 자동 실행하면 닫힌 상태로 실패해야 한다. 자격증명을 테스트 환경 변수에서 제거하며, 모델 패치는 빌드 스크립트나 다른 파일을 바꿀 수 없다.

2026-10-13 재부팅 후 읽기 전용 확인에서 개인 Codex 설정에는 `[permissions]`와 `repair-tests`가 없었다. 명령줄에 한 번만 적용하는 최소 프로필(`:minimal` 읽기, 해당 작업 checkout 쓰기, 네트워크 차단)은 설정 문법까지 확인했지만 이 실행 환경의 macOS는 중첩 sandbox 적용을 `Operation not permitted`로 거부했다. 지속 설정을 추가하거나 우회하지 않았다. 운영 호스트에서 검증하려면 작업 checkout 이외의 쓰기 권한을 주지 않으면서 JDK·Android SDK 읽기와 격리된 Gradle 캐시를 준비해야 한다. 공유 `~/.gradle`, Keychain, GitHub 인증, 개인 Codex 설정을 생성 테스트에 쓰기 허용하는 방식은 채택하지 않는다. 이 권한 경계와 격리 캐시가 실제 Gradle 검사에 충분한지 승인된 세션에서 확인하기 전에는 자동 복구 경로를 켜지 않는다.

`repair_publish.py`는 정확한 저장소·기준 브랜치·이슈 marker·GitHub 계정·write 권한·라벨·리뷰어·검증 commit을 다시 확인하고, 단일 commit만 force 없이 push한 뒤 Draft PR을 만든다. 결과가 불확실하면 기존 PR을 marker로 재조회하고 중복 생성하지 않는다. 여비는 승인된 상대 리뷰어가 있어야 한다. worker는 SQLite 작업 큐와 파일 lock으로 동시 실행·중복을 막고, 실패 작업을 다음 수신 또는 시작 시 재시도한다. 활성화 시각 이전의 기존 이슈는 자동 처리하지 않는다. 수신 ACK는 worker 결과와 분리돼 있다. 별도 재시도 타이머는 없으므로 조용한 기간의 실패 작업은 즉시 재시도되지 않는다.

실제 기준선 검사는 2026-10-13에 여비의 `ktlintCheck detekt :app:assembleDebug`가 통과했다. 반다라트의 `detekt allTests :androidApp:testDebugUnitTest :androidApp:assembleDebug`도 임시 디버그 서명 설정을 사용해 통과했고 임시 파일을 제거했다. 반다라트 전체 `spotlessCheck`는 수정하지 않은 `ObserveEvent.kt`의 기존 포맷 오류로 실패했다. 따라서 현재 전체 `PROJECT_CHECKS`는 반다라트에서 fail-closed이며 자동 Draft PR은 만들 수 없다. 어느 앱에서도 실제 앱 소유 결함에 대한 수정 전후 회귀 검증이나 Draft PR 게시를 완료했다고 주장하지 않는다.

2026-10-13 YeoBee-Dev 셸 주입 검증 이벤트는 Firebase MCP 이슈·샘플 이벤트의 `CrashedByAdbException`을 확인했고 [테스트 이슈 #561](https://github.com/YeoBee-official/YeoBee-Android/issues/561#issuecomment-6138733335)에 no-code-fix 판단을 남겼다. 코드 수정 PR은 만들지 않았다.

같은 이벤트를 여비 `develop` checkout에서 Codex CLI의 읽기 전용 sandbox와 Firebase MCP 읽기 도구 두 개로 분석했다. 구조화된 결과는 `issue_only` 및 `android.app.RemoteServiceException$CrashedByAdbException`이었다. 이 실행은 코드 수정·빌드·PR 경로를 검증하지 않는다. 합성 fixture는 회귀 테스트의 수정 전후 결과와 Draft PR 호출 경계를 검증하지만 실제 앱 결함 증거를 대신하지 않는다.
