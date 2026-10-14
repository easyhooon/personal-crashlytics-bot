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

`codex_repair.py`는 Codex CLI를 읽기 전용으로 실행하고, 허용 앱 ID와 이슈 ID만 전달해 Firebase MCP의 위 두 도구와 대상 소스를 읽힌다. 모델 결과는 구조화된 설명·회귀 테스트 패치·관련 Kotlin 소스 패치다. `repair_project.py`가 허용 파일·정확한 앱 stack 프레임·예외·소스 라인을 검사하고 별도로 테스트를 실행한다. 새 테스트가 수정 전 해당 예외로 실패하고 수정 후 통과해야 하며 실제 프로젝트 검사도 통과해야 한다. 생성된 테스트는 이 Mac에서 실행하지 않는다. `repair_ci.py`가 회귀 테스트 커밋과 테스트+수정 커밋을 각각 `crash-verify/**` 브랜치로 push하고, 대상 저장소의 시크릿 없는 `crash-repair-verify.yml` run 중 워크플로 경로·브랜치·head SHA가 정확히 일치하는 결과와 JUnit XML artifact만 근거로 쓴다. 모델 패치는 빌드 스크립트나 `.github`를 포함한 다른 파일을 바꿀 수 없다.

2026-10-13에 검토한 로컬 `codex sandbox -P repair-tests` 경로는 macOS 중첩 sandbox가 `Operation not permitted`로 거부해 검증하지 못했다. 2026-10-14에 사용자가 GitHub Actions 검증을 선택해 로컬 실행 경로를 제거했다.

`repair_publish.py`는 정확한 저장소·기준 브랜치·이슈 marker·GitHub 계정·write 권한·라벨·리뷰어·검증 commit을 다시 확인하고, CI에서 통과한 단일 commit을 그대로 force 없이 push한 뒤 Draft PR을 만든다. 결과가 불확실하면 기존 PR을 marker로 재조회하고 중복 생성하지 않는다. 여비는 승인된 상대 리뷰어가 있어야 한다. worker는 SQLite 작업 큐와 파일 lock으로 동시 실행·중복을 막고, 실패 작업을 다음 수신 또는 시작 시 재시도한다. 활성화 시각 이전의 기존 이슈는 자동 처리하지 않는다. 수신 ACK는 worker 결과와 분리돼 있다. 별도 재시도 타이머는 없으므로 조용한 기간의 실패 작업은 즉시 재시도되지 않는다.

검증 워크플로 smoke 결과(2026-10-14, 실제 GitHub Actions):
- 성공 경로: 여비 [run 38499876427](https://github.com/YeoBee-official/YeoBee-Android/actions/runs/38499876427), 반다라트 [run 38499925057](https://github.com/Nexters/BandalArt-KMP/actions/runs/38499925057)이 통과했다. 반다라트의 `spotlessCheck`는 `-PspotlessRatchetFrom=origin/main`으로 변경분만 검사해 기존 `ObserveEvent.kt` 포맷 오류에 막히지 않는다.
- 실패 경로: `IllegalStateException`을 던지는 임시 테스트를 넣은 커밋에서 여비 [run 38501426785](https://github.com/YeoBee-official/YeoBee-Android/actions/runs/38501426785), 반다라트 [run 38502055902](https://github.com/Nexters/BandalArt-KMP/actions/runs/38502055902)가 `failure`로 끝났고, 봇의 `Actions.reports`와 `matching_regression_failure`가 artifact XML에서 예외와 파일명을 찾았다.
- 반다라트 테스트가 컴파일되지 않은 run([38501432385](https://github.com/Nexters/BandalArt-KMP/actions/runs/38501432385))은 XML에 해당 테스트가 없어 재현으로 인정되지 않았다. 생성 테스트는 대상 소스셋의 기존 테스트 의존성과 detekt·spotless 규칙(반다라트는 라이선스 헤더 포함)을 따라야 한다.

이 워크플로는 여비 [#563](https://github.com/YeoBee-official/YeoBee-Android/pull/563)과 반다라트 [#438](https://github.com/Nexters/BandalArt-KMP/pull/438)로 2026-10-14에 각 기준 브랜치(`develop`, `main`)에 머지됐다. 워크플로가 없는 기준 브랜치에서는 `prepare_worktree`가 worktree를 만들기 전에 멈춘다. 2026-10-14 종단 간 검증: 여비 develop에 debug 전용 결함([#565](https://github.com/YeoBee-official/YeoBee-Android/pull/565))을 넣고 기기에서 크래시를 냈다. `PERSONAL_CRASHLYTICS_TEST_UNTIL`로 개발 앱을 임시 허용하고 `bot.py`를 수동 실행해 이슈 #566을 만들었다(Gmail 수신 단계는 미검증). `repair_executor.py`가 Codex 제안을 받아 커밋 A·B를 만들었고, A는 `NumberFormatException`·`AmountText.kt`로 실패([run](https://github.com/YeoBee-official/YeoBee-Android/actions/runs/38521796724)), B는 통과([run](https://github.com/YeoBee-official/YeoBee-Android/actions/runs/38522868819))했다. B 커밋 `020bcdfc2e8d`가 그대로 Draft PR [#567](https://github.com/YeoBee-official/YeoBee-Android/pull/567)의 HEAD가 됐고 기존 Android CI는 Draft라서 건너뛰었다.

이 과정에서 Codex는 이벤트를 읽은 실행 6번 중 5번 수정을 제안했고 1번은 셸 테스트 가능성을 들어 거부했다. 거부·도구 장애·형식 불일치는 모두 PR 없이 멈췄다. 운영 앱에서 자연 발생한 크래시로는 아직 검증하지 않았다.

2026-10-13 YeoBee-Dev 셸 주입 검증 이벤트는 Firebase MCP 이슈·샘플 이벤트의 `CrashedByAdbException`을 확인했고 [테스트 이슈 #561](https://github.com/YeoBee-official/YeoBee-Android/issues/561#issuecomment-6138733335)에 no-code-fix 판단을 남겼다. 코드 수정 PR은 만들지 않았다.

같은 이벤트를 여비 `develop` checkout에서 Codex CLI의 읽기 전용 sandbox와 Firebase MCP 읽기 도구 두 개로 분석했다. 구조화된 결과는 `issue_only` 및 `android.app.RemoteServiceException$CrashedByAdbException`이었다. 이 실행은 코드 수정·빌드·PR 경로를 검증하지 않는다. 합성 fixture는 회귀 테스트의 수정 전후 결과와 Draft PR 호출 경계를 검증하지만 실제 앱 결함 증거를 대신하지 않는다.
