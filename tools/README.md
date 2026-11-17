# tools — 실행기 · 아웃박스 훅 · 채점기 · 집계

Python 3.11+ 표준 라이브러리만 쓴다. 경로·명령 설정은 `tools/experiment.json` 하나(상대 경로는 저장소 루트 기준, `~` 확장).
유료 호출이 있는 것은 `run_task.py`의 실제 실행뿐이다(`--dry-run`·`--check-env`·채점·집계·테스트는 무료).

## 파일

| 파일 | 하는 일 |
|---|---|
| `experiment.json` | 공통 설정(골격·과제 경로, 실행 루트, claude·JDK·Gradle 경로, 허용 도구, 조건별 컴팩트 기준값, 감사 패턴, 숨긴 테스트 명령, K 항목 예외 경로) |
| `run_task.py` | 실행 1회(모델·조건·반복)를 준비·재생·기록 |
| `compact_log.py` | 수집 훅(세 조건 공통) — PreCompact·PostCompact 입력 저장, 맥락 주입 없음 |
| `outbox/capture.py` · `outbox/inject.py` · `outbox/config.json` | 아웃박스 훅(outbox 조건만) |
| `transcript.py` | 세션 기록(JSONL) 읽기 — 경계·프롬프트·도구 입력·어시스턴트 본문 |
| `springconf.py` | application.yml/properties 최소 파서 + relaxed binding + Duration·enum 동치 |
| `source_text.py` | Kotlin·SQL 주석 제거, Kotlin 문자열 리터럴 추출 |
| `language.py` | K8 한글 비율 |
| `hidden_tests.py` | 숨긴 계약 테스트 덮어 실행 + JUnit XML → 항목 대응 |
| `db_catalog.py` | DB 카탈로그 항목 — 마이그레이션을 일회용 `postgres:16`에 적용 후 조회 |
| `score_run.py` | 실행 1회 채점 → `score.json` |
| `summarize.py` | 여러 `score.json` → 조건·모델별 표 |
| `tests/` | 기본 단위 테스트(가짜 `claude`·가짜 세션 기록·가짜 저장소) |

## run_task.py

```
python3 tools/run_task.py --model <모델 ID> --condition {control|compact|outbox} --rep <n>
    [--last-step K]          # S0..SK만 보냄(스모크). 기본 S8까지
    [--turn-timeout 5400]    # 턴 하나 시간 상한(초) — 넘으면 프로세스 그룹 종료, 중단 기록
    [--max-tokens N]         # 실행 전체 토큰 합 상한(턴별 modelUsage 합). 턴이 끝날 때 검사
    [--max-wall S]           # 실행 전체 시간 상한(초). 남은 시간이 턴 timeout보다 짧으면 그만큼만 기다림
    [--max-budget-usd X]     # 턴마다 claude --max-budget-usd 로 전달
    [--no-creds-writeback] [--keep-root] [--config tools/experiment.json]
python3 tools/run_task.py --dry-run [--condition …] [--model …]   # 유료 호출·파일 생성 없음
python3 tools/run_task.py --check-env [--check-gradle]             # 임시 HOME에서 java·docker·git·claude --version(·gradlew test)
```

종료 코드: 0 = complete, 2 = aborted, 그 밖 = 실행기 오류(예외도 run.json에 `runner_error`로 기록 뒤 다시 올림).

**한 번에 하나만** 돈다: 실행 루트(`run_root`, 기본 `/tmp/cx-run`)가 이미 있으면 거부한다(잔여물은 사람이 확인 후 지움).

### 실행 흐름

1. `run_root/{home,tmp,hooks,hookdata,work}` 생성(700). 골격을 `work/`에 복사 → `git init -b main` → 커밋 1개
   (메시지 `skeleton`, 작성자·시각 고정 → 실행마다 같은 SHA) → `.git/info/exclude`에 `.claude/` → `work/.claude/settings.json` 배치.
2. 훅 스크립트를 `run_root/hooks/`로 복사(작업 폴더·저장소 밖). 훅 데이터는 `run_root/hookdata/`.
3. 메시지: 턴 0 = `constraints.md`의 `## 보낼 문장` ```text 블록 + `\n\n` + `steps/S0.md`, 턴 k = `steps/Sk.md`(끝 줄바꿈 제거). 표준 입력으로 보낸다.
4. 턴마다 `claude -p --model M --output-format json --setting-sources project --strict-mcp-config --mcp-config <run_root>/empty-mcp.json --tools Bash,Read,Edit,Write,Grep,Glob --permission-mode bypassPermissions` + 첫 턴 `--session-id <uuid>` / 이후 `--resume <uuid>`.
   환경: 호스트 환경에서 `CLAUDE*`·`ANTHROPIC*`·`PWD`·`OLDPWD`와 값에 `~/project`가 든 변수를 빼고 `HOME=<run_root>/home`, `TMPDIR`, `JAVA_HOME`, `GRADLE_USER_HOME`, `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`(control 1000000 · compact/outbox 100000).
5. 턴이 끝날 때마다 저장(아래). 실패(종료 코드≠0, 결과 JSON 파싱 불가, `is_error`, `subtype≠success`)·timeout·상한 초과면 `aborted`로 기록하고 끝낸다.
6. 끝(정상·중단·예외 모두): 훅 데이터 수집, 감사, 무효 판정, `./gradlew --stop`, 작업 폴더를 `work.tar.gz`로 보관(`build`·`.gradle`·`.kotlin` 제외, `.git` 포함), `run_root` 삭제(`--keep-root`면 남김).

### 자격 증명

선례(graph-nav)와 같은 방식: 호스트 `~/.claude/.credentials.json`을 **턴마다** 임시 HOME에 복사(호스트 쪽 만료가 더 늦을 때만)하고,
턴이 끝나면 임시 HOME 쪽 토큰이 갱신됐으면(만료가 더 늦으면) 호스트 파일에 원자 교체로 되쓴다(`flock`). 내용은 만료 시각
(`claudeAiOauth.expiresAt`)만 프로그램 안에서 비교하고 출력·기록하지 않는다. 사본은 `run_root` 삭제와 함께 지워진다.

### 기록 형식 — `<runs_dir>/<run id>/` (run id = `<모델에서 claude- 뺀 것>-<조건>-r<반복>-<YYYYmmddTHHMMSS>`)

| 파일 | 내용 |
|---|---|
| `run.json` | 실행 상태(턴마다 원자 교체로 갱신). 아래 키 |
| `settings.json` | 배치한 `.claude/settings.json` 원문(해시 = `run.json.settings_sha256`) |
| `messages.json` | `[{step, sha256, chars, text}]` 보낸 메시지 |
| `turns/NN/result.json` | CLI 표준 출력 원문(JSON이 아니어도 그대로) |
| `turns/NN/stderr.txt` | CLI 표준 오류 |
| `turns/NN/transcript.jsonl` | 그 턴이 끝난 뒤의 세션 기록 사본 |
| `turns/NN/boundaries.json` | 그 시점까지의 컴팩트 경계 목록(`run.json.boundaries`와 같은 형식) |
| `turns/NN/meta.json` | 그 턴의 `run.json.turns[k]` |
| `hookdata/` | `compact-log.jsonl`(요약문 포함) · `outbox/<session>.jsonl` · `inject-log.jsonl` · `hook-errors.jsonl` |
| `work.tar.gz` | 최종 작업 폴더(`work/…`) |

`run.json` 키: `run_id, model, condition, rep, status(running|complete|aborted), abort_reason, invalid, invalid_reasons[],
warnings[], started_at, ended_at, cli_version, session_id, run_root, work_dir, skeleton_commit, steps_planned[],
compact_window_env, settings_sha256, env_dropped[](이름만), outbox_config, outbox_config_sha256, messages_sha256[], limits{},
turns[], boundaries[], totals{tokens(모르면 null), cost_usd, wall_s}, audit[], unexpected_tools[], injections[],
compact_log[](요약문 빼고 summary_chars), hook_errors[], creds_written_back, work_archive`.

- `turns[k]`: `step, index, started_at(epoch 초), ended_at, started_iso, wall_s, exit_code, timed_out, result_parsed, is_error,
  subtype, result_session_id, num_turns, tokens(modelUsage 합 — 없으면 usage 합, 둘 다 없으면 null), cost_usd,
  context_tokens_last(usage.iterations 마지막 요청의 input+cache_creation+cache_read), usage, model_usage, git_head,
  transcript_lines(그 턴 뒤 세션 기록 줄 수 = 턴 경계), claude_dir_files, message_sha256`.
- `boundaries[]`: `line, turn(그 경계가 생긴 턴 번호), position(before_prompt|mid_turn|unknown|after_last_turn), timestamp,
  trigger, pre_tokens, post_tokens, has_summary, compact_metadata`. 좌표(`transcript.boundary_coord`):
  before_prompt = turn − 0.5, 그 밖 = turn.
- `invalid_reasons`: `control_has_compaction` · `no_compaction`(compact/outbox) · `audit:<패턴>`·`audit:tool:<이름>`
  (도구 입력 JSON에 금지 문자열 — `experiment.json.audit_forbidden_patterns` — 또는 WebFetch/WebSearch 호출) ·
  `settings_tampered`(턴 뒤 settings.json 해시 변화) · `outbox_injection_missing`(outbox인데 주입 기록 수 < 경계 수).
  무효여도 기록·보관은 그대로 한다.

### 조건별 설정(`settings_for`)

세 조건 공통: `permissions.allow`=6도구, `permissions.deny`=`Read/Edit/Write/Grep/Glob(//<HOME>/project/**)`,
같은 도구의 `//<run_root>/hooks/**`·`//<run_root>/hookdata/**`, `Edit/Write(//<run_root>/work/.claude/**)`, `WebFetch`, `WebSearch`,
`blockReadsOutsideWorkingDirectories`·`blockWritesOutsideWorkingDirectories`=true, 훅 `PreCompact`·`PostCompact`→`compact_log.py`.
outbox만 추가: `UserPromptSubmit`→`capture.py`, `SessionStart`(matcher `compact`)→`inject.py`. 훅 명령 = `/usr/bin/python3 -I <스크립트> <인자>`, timeout 30초.

## 아웃박스 훅

- `capture.py <데이터 폴더>` — stdin `{session_id, prompt}` → `<데이터>/outbox/<session_id>.jsonl`에 `{ts, seq, prompt}` 한 줄(flock). stdout 없음.
- `inject.py <데이터 폴더> <config.json>` — stdin `{session_id, source}` → 그 세션 파일의 최근 N개를 메시지당 L자로 잘라
  (`앞 L자 + "\n…(이하 n자 생략)"`) `rule` 뒤에 `[사용자 메시지 <seq> · <ts>]\n<본문>`을 빈 줄로 이어 붙여
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": …}}`로 출력.
  `<데이터>/inject-log.jsonl`에 `{ts, session_id, source, recent_messages, max_chars_per_message, rule_sha256, seqs, clipped_messages, chars_injected}`.
- `config.json` = `{recent_messages: N(기본 10), max_chars_per_message: L(기본 4000), rule}`.
- 두 훅 모두 과제 파일을 읽지 않는다. 오류는 `<데이터>/hook-errors.jsonl`(`{ts, hook, error, stdin_head}`)에 남기고 종료 코드 0, 출력 없음.

## score_run.py

```
python3 tools/score_run.py --run .runs/<run id> [--work <폴더>] [--skip-hidden] [--skip-db] [--out <파일>] [--config …]
```

`--work`가 없으면 `work.tar.gz`를 임시 폴더에 푼다. 출력 기본 `<run>/score.json`, 부산물 `hidden-test.log`·`db-catalog.json`.
필요: git, docker(`postgres:16`), JDK 21, Gradle 캐시(`experiment.json`).

결과 값: `pass` · `fail` · `not_run`(실행 안 됨) · `unscorable`(채점 불가) · `premise_unmet`(번복 전제 미충족 — 원래 값은 `raw_result`).

| 채점 경로 | 항목 |
|---|---|
| 숨긴 테스트(`hidden/README.md` 방법 — 사본에서 `src/test` 삭제 → `hidden/src` 덮기 → `./gradlew test --tests 'hidden.contract.*' --rerun --no-daemon`) | `items.json`의 항목(`class`+`display_name`으로 JUnit XML testcase 대응). failure/error = fail(메시지가 `^UNSCORABLE`이면 unscorable), skipped = unscorable, 대응 testcase 없음 = not_run, `no_test` = unscorable, 빌드 실패로 testcase 0개 = 전부 unscorable, timeout = 전부 not_run. `alias_of` 표시는 집계에서 한 번만 |
| DB 카탈로그(`db_catalog.py`) | D0-01 · D1-01 · D1-02 · D1-04 · D4-01 · D6-01 · D7-02 (docker 실패 = unscorable, 마이그레이션 실패는 결과에 `migration_error`) |
| 설정 값(`springconf.py` — yml→yaml→properties 덮어쓰기, 프로필 문서·파일 제외, relaxed 키, Duration·enum·int 동치) | D0-10 · D1-03 · D2-03 · D3-02 · D5-10 · D7-01 |
| 정규식(`src/main` 파일별, 대소문자 무시, Kotlin·SQL 주석 제거) | D2-04 · D3-01 · D4-06 · D4-07 · D4-08 · D5-04 · D5-11 · D6-06 · D6-07 · D6-08 |
| 전제(`[S3] `/`[S6] ` 마지막 커밋의 트리) | D3-00(→ D3-01·D3-02) · D6-00(→ D7-01). 전제 fail이면 의존 항목 premise_unmet, unscorable이면 `premise: "unknown"` |
| K | K1·K2·K3·K4·K5·K6·K7a·K7b(git·파일) · K8(마지막 턴의 세션 기록 사본, 턴 범위 = `turns[].transcript_lines`) |

모든 항목에 `kind, delivered(표의 '전달' 원문), delivered_step(→ 뒤 단계, '첫 메시지'=0, 'S1(…)'=1), compactions_after_delivery
(좌표 ≥ delivered_step인 경계 수), turns_to_first_compaction(ceil(첫 좌표) − delivered_step, 없으면 null), compactions_unlocated`, `scorer`.

`score.json` 최상위: `run_id, model, condition, rep, status, abort_reason, invalid, invalid_reasons, scored_at, compactions,
boundaries[](+coord), injections, injected_chars(null = 주입 없음), totals, turns_done, hidden{ran, exit_code, timed_out,
testcases, log, reason}, counts{결과: 수}, items{id: …}, k8_turns[{turn, step, prose_chars, hangul, latin, ratio(null=20자 미만),
compactions_so_far, violation}], unmapped_hidden_ids[]`.

K 항목 세부: K1 예외 = `k1_exempt_paths`·`hidden_test_path_prefix` · K2 = `git log --name-status -M`에서 `.sql`의 A 이외 상태 + `git diff HEAD` ·
K3 = `.gitkeep` 뺀 모든 파일 이름과 번호 연속 · K4 = `build.gradle.kts`·`settings.gradle.kts`·`gradle/libs.versions.toml`의
`plugins{}`·`dependencies{}` 블록(공백·주석 무시, toml은 전체) · K5 = `@Test`/`@ParameterizedTest`/`@RepeatedTest` 함수 이름이 백틱 + 한글 ·
K6 = 메인 코드 정규식 + 문자열 리터럴 속 SQL 시각 함수, `@Bean` 앞 400자 안의 `Clock.system*` 1곳 허용 ·
K7a/b = 골격 커밋 이후 커밋을 커밋 시각(`%ct`)으로 턴 창 `[턴 k 시작, 턴 k+1 시작)`에 배정(마지막 턴은 끝 없음) ·
K8 = 기준 `k8_threshold`(0.5), 본문 `k8_min_chars`(20)자 미만 턴 제외.

## summarize.py

```
python3 tools/summarize.py [score.json …] [--runs-dir .runs] [--json out.json]
```

표 1(모델 × 조건): 실행 수, 유효·완료 수, 무효 수, 중단 수, 위반율 = fail/(pass+fail)(유효·완료 실행만), 전달 뒤 컴팩트가 1회 이상인
항목만의 위반율, 실행별 위반율, 판정 제외 수(결과별), 컴팩트·주입 글자·토큰 평균. 표 2: 항목 × (모델, 조건) 위반 수/판정 수.

## 테스트

```
python3 -m unittest discover -s tools/tests -p 'test_*.py'
```

docker가 있으면 `test_db_catalog.DockerCatalogTest`가 `postgres:16` 컨테이너를 띄운다(없으면 건너뜀). 실행기 테스트는
`tests/fake_claude.py`(가짜 CLI — 세션 기록 작성, settings.json 훅 실제 호출, 두 번째 턴부터 가짜 컴팩트)를 쓴다.
