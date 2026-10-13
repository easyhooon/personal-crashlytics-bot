# 크래시 수정 판단 경계

현재 운영 수신기는 인증된 Crashlytics 메일을 개인 GitHub 이슈로 기록한다. 자동 코드 수정과 Draft PR 생성기는 아직 운영 경로에 연결되지 않았다. `bot.py --probe`는 README를 바꾸는 합성 연결 검사이며 크래시 수정 경로가 아니다.

`crashlytics_mcp.py`는 Firebase CLI의 공식 MCP 서버에서 `crashlytics_get_issue`와 `crashlytics_list_events` 두 읽기 전용 도구만 호출한다. 입력 앱과 이슈 ID는 `bot.TARGETS`로 검증한다. 샘플 이벤트에는 로그와 breadcrumb가 포함될 수 있으므로 원문을 stdout, GitHub, 저장소 파일에 출력하지 않는다. MCP 인증 실패·이슈 불일치·샘플 누락은 분석을 중단한다.

`repair_triage.py`는 확인된 사실과 미확인 사항을 기존 이슈에 남긴다. `CrashedByAdbException`이 Firebase 이슈와 샘플 이벤트에 모두 있으면 셸 주입 테스트로 분류하고 앱 코드 수정 없이 종료한다. 그 밖의 원인은 현재 `needs-investigation`으로 남긴다. 분석 코멘트는 Firebase 이슈와 GitHub 이슈 marker가 일치할 때만 작성하며, 분류별 marker로 재실행 시 중복을 막는다.

```bash
python3 -B repair_triage.py --alert-file PRIVATE_NORMALIZED_ALERT.json --issue-number NUMBER --dry-run
python3 -B repair_triage.py --alert-file PRIVATE_NORMALIZED_ALERT.json --issue-number NUMBER
```

두 번째 명령은 이슈 코멘트를 실제로 작성한다. 운영 LaunchAgent에는 아직 등록하지 않았다.

`repair_policy.assess`는 수정 PR 후보의 최소 조건을 검사한다. Firebase 샘플 이벤트, 앱 소유 stack 프레임과 같은 소스 파일의 변경, 앱 코드가 원인인 구체적 설명, 수정 전 실패한 회귀 테스트, 수정 후 통과한 회귀 테스트와 실제 프로젝트 검사, 검사한 commit SHA가 모두 필요하다. 모델의 확신 점수나 자체 테스트 통과 문장은 조건이 아니다. 셸 주입, 외부 원인, 증거 부족, 관련 없는 변경, 검증 불가에서는 `issue_only`를 반환한다. 제조사 한정 보고 자체는 배제 조건이 아니며 같은 앱 코드 근거와 검증 조건을 적용한다.

현재 `assess`는 안전성 판단 함수다. Codex의 코드 수정 실행, 독립적인 전후 테스트 수집, 대상 저장소별 작업 규칙 적용, commit·push·Draft PR 게시를 수행하는 실행기는 아직 없다. 따라서 이 문서와 테스트만으로 자동 수정 PR이 동작한다고 판단하면 안 된다. 여비의 실제 앱 빌드·테스트 결과와 반다라트의 실제 KMP 검사도 아직 수집하지 않았다. 분석/수정 실행기가 완성되고 대상별 실제 프로젝트 검증을 거친 뒤에만 운영 수신기에서 호출한다.

2026-10-13 YeoBee-Dev 셸 주입 검증 이벤트는 Firebase MCP 이슈·샘플 이벤트의 `CrashedByAdbException`을 확인했고 [테스트 이슈 #561](https://github.com/YeoBee-official/YeoBee-Android/issues/561#issuecomment-6138733335)에 no-code-fix 판단을 남겼다. 코드 수정 PR은 만들지 않았다.

같은 이벤트를 여비 `develop` checkout에서 Codex CLI의 읽기 전용 sandbox와 Firebase MCP 읽기 도구 두 개로 분석했다. 구조화된 결과는 `issue_only` 및 `android.app.RemoteServiceException$CrashedByAdbException`이었다. 이 실행은 코드 수정·빌드·PR 경로를 검증하지 않는다.
