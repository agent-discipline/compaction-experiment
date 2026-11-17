# tools — 실행기 · 아웃박스 훅 · 채점기 · 집계

Python 3.11+ 표준 라이브러리만 쓴다. 경로·명령 설정은 `tools/experiment.json` 하나(상대 경로는 저장소 루트 기준, `~` 확장).
유료 호출이 있는 것은 `run_task.py`의 실제 실행뿐이다(`--dry-run`·`--check-env`·채점·집계·테스트는 무료).
에이전트와 숨긴 테스트는 **컨테이너 안**에서 돈다(2026-11-18 결정 — 명세 "승인 상태"의 실행 환경 결정). 호스트에는 Docker만 있으면 된다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `experiment.json` | 공통 설정(골격·과제 경로, 실행 루트, 이미지·토큰 파일·Docker 접두, 컨테이너 안 경로·자원 상한, DB 사이드카, 허용 도구, 조건별 컴팩트 기준값, 감사 패턴, 숨긴 테스트 명령, K 항목 예외 경로) |
| `agent-image/Dockerfile` | 에이전트 이미지(채점 컨테이너도 같은 이미지) |
| `containers.py` | Docker 실행 계층 — 모든 docker CLI 호출, 이름 접두 검사, PostgreSQL 사이드카(`PostgresStack`) |
| `run_task.py` | 실행 1회(모델·조건·반복)를 준비·재생·기록 |
| `compact_log.py` | 수집 훅(세 조건 공통) — PreCompact·PostCompact 입력 저장, 맥락 주입 없음 |
| `outbox/capture.py` · `outbox/inject.py` · `outbox/config.json` | 아웃박스 훅(outbox 조건만) |
| `transcript.py` | 세션 기록(JSONL) 읽기 — 경계·프롬프트·도구 입력·어시스턴트 본문 |
| `springconf.py` | application.yml/properties 최소 파서 + relaxed binding + Duration·enum 동치 |
| `source_text.py` | Kotlin·SQL 주석 제거, Kotlin 문자열 리터럴 추출 |
| `language.py` | K8 한글 비율 |
| `hidden_tests.py` | 숨긴 계약 테스트 덮어 실행(실행 함수는 호출자가 줌) + JUnit XML → 항목 대응 |
| `db_catalog.py` | DB 카탈로그 항목 — 채점 사이드카의 새 데이터베이스에 마이그레이션 적용 후 조회 |
| `score_run.py` | 실행 1회 채점 → `score.json` |
| `summarize.py` | 여러 `score.json` → 조건·모델별 표 |
| `tests/` | 단위 테스트(가짜 `docker`·가짜 `claude`·가짜 세션 기록·가짜 저장소) |

## 에이전트 이미지

```bash
docker build -t cx-agent:2.1.326 --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
    -f tools/agent-image/Dockerfile tasks/seat-reservation/skeleton
```

- 기반: `node:22-bookworm-slim` + `eclipse-temurin:21-jdk-jammy`의 JDK 21 + git·python3 + `npm install -g @anthropic-ai/claude-code@2.1.326`
  (need-harness-lab 선례와 같은 방식). `DISABLE_AUTOUPDATER=1`.
- 사용자 `agent`(root 아님), uid·gid = 빌드 인자(호스트 uid). 레이블 `cx.uid`·`cx.gid`·`cx.claude-code` — 실행기가 호스트 uid와 다르면 거부.
- `HOME=/home/agent`, `CLAUDE_CONFIG_DIR=/home/agent/.claude`, `GRADLE_USER_HOME=/opt/gradle-home`, `WORKDIR /work`.
- **빌드 컨텍스트는 골격 폴더뿐**(단계 메시지·제약·채점·숨긴 테스트는 컨텍스트 밖). 버려지는 중간 단계에서 골격을
  `build testClasses -x test` + 모든 resolvable configuration 해석으로 한 번 빌드해 Gradle 배포판·의존성 캐시만 최종 이미지로 복사한다
  (골격 자체는 이미지에 남지 않음). 골격 의존성을 바꾸면 다시 빌드한다. BuildKit 없이(클래식 빌더) 빌드된다.
- 측정(2026-11-18): id `sha256:0a6a9adef904…`, 1.88GB. 빌드 약 3분 44초 = JDK·apt·npm·사용자 단계 51.6초(첫 시도, 캐시 없음) + 그 뒤 예열 단계(Gradle 2분 23초)·복사 172초(앞 단계는 캐시).

## run_task.py

```
python3 tools/run_task.py --model <모델 ID> --condition {control|compact|outbox} --rep <n>
    [--last-step K]          # S0..SK만 보냄(스모크). 기본 S8까지
    [--turn-timeout 5400]    # 턴 하나 시간 상한(초) — 넘으면 docker kill + 클라이언트 종료, 중단 기록
    [--max-tokens N]         # 실행 전체 토큰 합 상한(턴별 modelUsage 합). 턴이 끝날 때 검사
    [--max-wall S]           # 실행 전체 시간 상한(초). 남은 시간이 턴 timeout보다 짧으면 그만큼만 기다림
    [--max-budget-usd X]     # 턴마다 claude --max-budget-usd 로 전달
    [--keep-root] [--allow-concurrent] [--config tools/experiment.json]
python3 tools/run_task.py --dry-run [--condition …] [--model …]   # 유료 호출·파일·Docker 리소스 생성 없음(이미지 inspect만)
python3 tools/run_task.py --check-env [--check-gradle]             # 무료: 이미지·토큰 파일(경로·권한만)·컨테이너 안 버전·격리·훅
```

종료 코드: 0 = complete, 2 = aborted, 3 = failed(토큰 검출·검사 오류·보관 실패·Docker 정리 실패), 그 밖 = 실행기 오류(예외도 run.json에 `runner_error`로 기록 뒤 다시 올림).

`--check-env`: 컨테이너(`--network none`) 안에서 `claude --version`(2.1.326), java·git·python3 버전, 호스트 HOME·`~/project`·저장소 경로·
`/var/run/docker.sock`·`/run/docker.sock`의 존재(전부 absent여야 함), 마운트 목록, 훅 3개를 가짜 입력으로 실행. `--check-gradle`을 더하면
내부 전용 네트워크(`--internal` — 외부 접속 없음) + 사이드카에서 골격 사본으로 `./gradlew test --rerun --offline`을 두 번 돌리고, 실행마다
사이드카의 `test_*` 데이터베이스와 기본 DB의 `flyway_schema_history` 유무를 보여 준다.

**한 번에 하나만** 돈다: 실행 루트(`run_root`, 기본 `/tmp/cx-run`)가 이미 있거나, 같은 이름의 Docker 리소스가 있거나, 레이블
`cx.experiment=compaction-experiment` 컨테이너가 있으면 거부한다(잔여물은 사람이 확인 후 지움). 이름이 `payment-`로 시작하는 컨테이너가
**돌고 있으면** 거부한다(명세 §2 — need-harness-lab 유료 실행 중에는 돌리지 않음. `--allow-concurrent`로만 넘김).

### 격리 구성

| 무엇 | 어디 | 컨테이너 안 |
|---|---|---|
| 작업 폴더(유일한 쓰기 가능 호스트 경로) | bind `<run_root>/work` | `/work` |
| 훅 스크립트·`outbox-config.json`·`empty-mcp.json` | bind `<run_root>/hooks`, **readonly** | `/cx/hooks` |
| HOME(`~/.claude` 세션 기록·`.gitconfig`) | 볼륨 `cx-<run>-home`(턴 사이 유지) | `/home/agent` |
| 훅 데이터 | 볼륨 `cx-<run>-hookdata` | `/cx/hookdata` |
| 테스트 DB | 컨테이너 `cx-<run>-db`(`postgres:16 -c fsync=off -c max_connections=500`, 데이터 볼륨 `cx-<run>-pgdata`) | 호스트 이름 `db:5432`, DB·사용자·암호 `booking` |
| 네트워크 | `cx-<run>-net`(일반 bridge — 외부 접속 가능: 모델 API·빌드 의존성) | 에이전트 컨테이너 + 사이드카만 |

- 마운트하지 않는 것: 호스트 HOME·`/home/jun/project/**`·Docker 소켓. `--privileged`·DinD 없음. `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
  `--init`, `--user <호스트 uid>:<gid>`, `--cpus 4`·`--memory 6g`·`--pids-limit 2048`(세 조건 같음).
- 환경(토큰 제외, 세 조건 같고 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`만 다름): `HOME`, `CLAUDE_CONFIG_DIR`, `DISABLE_AUTOUPDATER=1`,
  `DB_URL=jdbc:postgresql://db:5432/booking`, `DB_USERNAME`, `DB_PASSWORD`. 호스트 환경 변수는 하나도 넘어가지 않는다.
- 이름: 모든 리소스가 `cx-<run id 소문자>-…`(접두 `resource_prefix`)이고 레이블 `cx.experiment=compaction-experiment`·`cx.run=<태그>`를 단다.
  지우기는 실행기가 만든 **정확한 이름**만(`containers.Docker.remove` — 접두가 다르거나 `payment-`로 시작하면 예외). prune·필터 삭제 없음.

### 실행 흐름

1. 확인: 토큰 파일 형식, 이미지(uid 레이블), 잔여 리소스·동시 실행(위). 무엇도 만들기 전.
2. `run_root/{hooks,work,scratch}` 생성(700). 골격을 `work/`에 복사 → `git init -b main` → 커밋 1개(메시지 `skeleton`, 작성자·시각 고정,
   호스트 git 설정 무시 → 실행마다 같은 SHA) → `.git/info/exclude`에 `.claude/` → `work/.claude/settings.json` 배치. 훅 스크립트를 `hooks/`로 복사.
3. Docker: 네트워크 → 볼륨 3개 → DB 사이드카(`pg_isready` 대기) → `cx-<run>-init`(네트워크 없음)로 HOME 볼륨 초기화(`~/.claude` 폴더,
   `git config --global user.name/email`(골격 커밋과 같은 값)·`init.defaultBranch main`) → `claude --version` 기록.
4. 메시지: 턴 0 = `constraints.md`의 `## 보낼 문장` ```text 블록 + `\n\n` + `steps/S0.md`, 턴 k = `steps/Sk.md`(끝 줄바꿈 제거).
5. 턴마다 `docker create cx-<run>-tNN … cx-agent:2.1.326 claude -p --model M --output-format json --setting-sources project --strict-mcp-config
   --mcp-config /cx/hooks/empty-mcp.json --tools Bash,Read,Edit,Write,Grep,Glob --permission-mode bypassPermissions` + 첫 턴 `--session-id <uuid>` /
   이후 `--resume <uuid>` → `docker start --attach --interactive`(메시지 = 표준 입력) → `docker inspect`(State만: 종료 코드·OOMKilled) →
   `docker cp`로 `~/.claude/projects`(세션 기록)와 `/cx/hookdata/.` 회수 → `docker rm -f`.
6. 턴이 끝날 때마다 저장(아래). 실패(종료 코드≠0, OOMKilled, 결과 JSON 파싱 불가, `is_error`, `subtype≠success`, 세션 기록 없음)·timeout·상한 초과면
   `aborted`로 기록하고 끝낸다.
7. 끝(정상·중단·예외 모두): 수집 컨테이너(만들기만, 실행 안 함)로 HOME 볼륨의 `~/.claude` 전체 사본 → **토큰 검사**(아래) → 훅 데이터 수집·감사·무효 판정 →
   `work.tar.gz`(`build`·`.gradle`·`.kotlin` 제외, `.git` 포함)·`claude-home.tar.gz` 보관 → Docker 리소스 정리(턴·init·collect·DB 컨테이너 →
   네트워크 → 볼륨) → `run_root` 삭제(`--keep-root`면 남김). 정리 오류는 `cleanup_errors`에 남고 실행은 `failed`.

### 인증·토큰

- `~/.config/payment-lab/claude.env`(`token_env_file`, 한 줄 `CLAUDE_CODE_OAUTH_TOKEN=…`, 권한 600, 저장소 밖)를 **`docker create --env-file`로만** 넘긴다.
  실행기는 형식 확인과 검사용으로만 값을 메모리에 읽고 출력·기록하지 않는다(`--dry-run`·`--check-env`는 경로·권한만 보고 내용은 읽지 않음).
  호스트의 `~/.claude/.credentials.json`은 읽지도 복사하지도 않는다. `docker inspect`는 State만 읽는다(Env 미보관).
- 토큰 검사(`scrub`): 끝에서 `run_root/work`, `~/.claude` 사본, 실행 기록 폴더 전체(파일 내용·이름)를 토큰 값으로 검사해 찾으면 `<REDACTED-SECRET>`로 바꾸고
  `SECRET-LEAK.txt`(경로 목록)를 남긴 뒤 실행을 `failed`(`secret_leak`)로 한다. 검사 자체가 실패해도 `failed`(`secret_scan_error`).
  마지막 `run.json`을 쓴 뒤 기록 폴더를 한 번 더 검사한다. 압축본은 압축 전 폴더를 검사한 뒤 만든다.

### 기록 형식 — `<runs_dir>/<run id>/` (run id = `<모델에서 claude- 뺀 것>-<조건>-r<반복>-<YYYYmmddTHHMMSS>`)

| 파일 | 내용 |
|---|---|
| `run.json` | 실행 상태(턴마다 원자 교체로 갱신). 아래 키 |
| `settings.json` | 배치한 `.claude/settings.json` 원문(해시 = `run.json.settings_sha256`) |
| `messages.json` | `[{step, sha256, chars, text}]` 보낸 메시지 |
| `turns/NN/result.json` | CLI 표준 출력 원문(JSON이 아니어도 그대로) |
| `turns/NN/stderr.txt` | CLI 표준 오류(+ docker 클라이언트 오류) |
| `turns/NN/transcript.jsonl` | 그 턴이 끝난 뒤의 세션 기록 사본 |
| `turns/NN/boundaries.json` | 그 시점까지의 컴팩트 경계 목록(`run.json.boundaries`와 같은 형식) |
| `turns/NN/meta.json` | 그 턴의 `run.json.turns[k]` |
| `turns/NN/container.json` | `{name, state{exit_code, oom_killed, status, error}, start_exit_code}` |
| `hookdata/` | `compact-log.jsonl`(요약문 포함) · `outbox/<session>.jsonl` · `inject-log.jsonl` · `hook-errors.jsonl` |
| `work.tar.gz` | 최종 작업 폴더(`work/…`) |
| `claude-home.tar.gz` | 끝난 뒤 HOME 볼륨의 `~/.claude` 전체(`claude-home/…` — 세션 기록·tool-results 등) |
| `SECRET-LEAK.txt` | 토큰 검출 시에만 |

`run.json` 키: `run_id, model, condition, rep, status(running|complete|aborted|failed), abort_reason, failure_reasons[], invalid, invalid_reasons[],
warnings[], started_at, ended_at, cli_version(컨테이너 안), session_id, image, image_id, image_labels, docker{리소스 이름}, token_env_file(경로만),
run_root, work_dir(/work), skeleton_commit, steps_planned[], compact_window_env, settings_sha256, container_env{토큰 제외}, container_limits,
outbox_config, outbox_config_sha256, messages_sha256[], limits{}, turns[], boundaries[], totals{tokens(모르면 null), cost_usd, wall_s}, audit[],
unexpected_tools[], injections[], compact_log[](요약문 빼고 summary_chars), hook_errors[], cleanup_errors[], secret_scan{hits{work, claude-home, record}, error,
late_hits?}, work_archive, claude_home_archive`.

- `turns[k]`: `step, index, started_at(epoch 초), ended_at, started_iso, wall_s, exit_code, timed_out, container, result_parsed, is_error,
  subtype, result_session_id, num_turns, tokens(modelUsage 합 — 없으면 usage 합, 둘 다 없으면 null), cost_usd,
  context_tokens_last(usage.iterations 마지막 요청의 input+cache_creation+cache_read), usage, model_usage, git_head,
  transcript_lines(그 턴 뒤 세션 기록 줄 수 = 턴 경계), claude_dir_files, message_sha256`.
- `boundaries[]`: `line, turn(그 경계가 생긴 턴 번호), position(before_prompt|mid_turn|unknown|after_last_turn), timestamp,
  trigger, pre_tokens, post_tokens, has_summary, compact_metadata`. 좌표(`transcript.boundary_coord`):
  before_prompt = turn − 0.5, 그 밖 = turn.
- `invalid_reasons`: `control_has_compaction` · `no_compaction`(compact/outbox) · `audit:<패턴>`·`audit:tool:<이름>`
  (도구 입력 JSON에 금지 문자열 — `experiment.json.audit_forbidden_patterns`(`{real_home}`·`{hooks}`·`{hookdata}` 치환) — 또는 WebFetch/WebSearch 호출) ·
  `settings_tampered`(턴 뒤 settings.json 해시 변화) · `outbox_injection_missing`(outbox인데 주입 기록 수 < 경계 수).
  무효여도 기록·보관은 그대로 한다. `failed`(실행기 쪽 실패)와 `invalid`(측정 무효)는 별개다.

### 조건별 설정(`settings_for` — 경로는 컨테이너 안)

세 조건 공통: `permissions.allow`=6도구, `permissions.deny`=`Read/Edit/Write/Grep/Glob(//cx/hooks/**)`·`(//cx/hookdata/**)`,
`Edit/Write(//work/.claude/**)`, `WebFetch`, `WebSearch`, `blockReadsOutsideWorkingDirectories`·`blockWritesOutsideWorkingDirectories`=true,
훅 `PreCompact`·`PostCompact`→`compact_log.py`. outbox만 추가: `UserPromptSubmit`→`capture.py`, `SessionStart`(matcher `compact`)→`inject.py`.
훅 명령 = `/usr/bin/python3 -I /cx/hooks/<스크립트> /cx/hookdata …`(컨테이너 안 python), timeout 30초. 도구 표면은 `--tools`로 고정(6개),
`bypassPermissions`는 컨테이너 안에서만 쓴다(호스트 경로 deny 규칙은 없앰 — 호스트가 보이지 않으므로).

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
필요: git(호스트), Docker + 에이전트 이미지 + `postgres:16`. 호스트 JDK·Gradle은 쓰지 않는다.

채점 환경(`containers.PostgresStack`, 숨긴 테스트·DB 카탈로그 중 하나라도 돌 때): 네트워크 `cx-score-<hex>-net` + 사이드카 `cx-score-<hex>-db`
(데이터 tmpfs) → 끝나면 둘 다 지움. 숨긴 테스트는 **채점 컨테이너**(`docker run --rm`, 같은 에이전트 이미지, 토큰 없음, 사본 폴더 bind 하나 → `/work`,
`DB_URL=jdbc:postgresql://db:5432/booking` 등)에서 돈다. `hidden.egress`(기본 true)가 false면 네트워크를 `--internal`로 만든다 — 골격 의존성만
쓰면 내부 전용으로도 통과(2026-11-18 확인)하지만, 에이전트가 의존성을 더한 실행(K4 위반)은 외부 없이는 빌드가 안 되어 H 항목이 전부
채점 불가가 되므로 기본은 외부 허용. `score.json.scoring_env`에 `{used, tag, image, image_id, db_image, egress, cleanup_errors, error}`.
정리 오류가 있으면 종료 코드 1.

결과 값: `pass` · `fail` · `not_run`(실행 안 됨) · `unscorable`(채점 불가) · `premise_unmet`(번복 전제 미충족 — 원래 값은 `raw_result`).

| 채점 경로 | 항목 |
|---|---|
| 숨긴 테스트(`hidden/README.md` 방법 — 호스트 사본에서 `src/test` 삭제 → `hidden/src` 덮기 → 채점 컨테이너에서 `./gradlew test --tests 'hidden.contract.*' --rerun --no-daemon`) | `items.json`의 항목(`class`+`display_name`으로 JUnit XML testcase 대응). failure/error = fail(메시지가 `^UNSCORABLE`이면 unscorable), skipped = unscorable, 대응 testcase 없음 = not_run, `no_test` = unscorable, 빌드 실패로 testcase 0개 = 전부 unscorable, timeout = 전부 not_run. `alias_of` 표시는 집계에서 한 번만 |
| DB 카탈로그(`db_catalog.py` — 채점 사이드카에 새 DB `catalog_<hex>`, `docker exec … psql`로 파일을 표준 입력으로 적용) | D0-01 · D1-01 · D1-02 · D1-04 · D4-01 · D6-01 · D7-02 (docker 실패 = unscorable, 마이그레이션 실패는 결과에 `migration_error`) |
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

실행기 테스트는 Docker 없이 돈다: `cfg["docker_bin"]`을 `tests/fake_docker.py`(가짜 docker — create·start·cp·inspect·rm·network·volume·ps를
흉내, 마운트 대상 경로를 호스트 폴더로 바꿔 명령을 호스트에서 실행)로 바꾸고, 컨테이너 안 `claude`는 `tests/fake_claude.py`(세션 기록 작성,
settings.json 훅 실제 호출, 두 번째 턴부터 가짜 컴팩트, 토큰 유출·턴 실패 흉내)가 된다. docker가 있으면 `test_db_catalog.DockerCatalogTest`가
`PostgresStack`(실제 `postgres:16`)을 띄운다(없으면 건너뜀).
