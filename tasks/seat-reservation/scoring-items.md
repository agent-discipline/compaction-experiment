# 채점 항목 (task 01 — E-consolidate 정리본)

- 대상: 실행이 끝난 최종 저장소(작업 트리) · 커밋 이력 · 세션 기록.
- 종류: **H** = 숨긴 계약 테스트(HTTP + DB, 채점 컨테이너 + PostgreSQL 16 사이드카) · **M** = 정적 표지(파일·정규식·설정 값·DB 카탈로그) · **G** = 커밋 이력 · **L** = 응답 언어.
- "전달" 열: 그 결정을 전달한 단계(대화 전용 제약은 S0 앞 첫 메시지). 모든 항목에 붙이고, 집계 때 "그 뒤 첫 컴팩트까지의 거리"를 이 값으로 계산한다.
- "대체" 열: 나중 단계가 바꾼 항목은 **최종 상태가 새 결정을 따르는지**로 채점한다(옛 결정이 남아 있으면 위반).
- "취약" 열: ● = 구현이 정당하게 달라도 깨지거나 옛 구현으로도 참일 수 있어 파일럿 뒤 버릴 후보 · ◐ = 조건부로 튼튼(전제 표기) · 빈칸 = 튼튼.
  채점기는 ● 표시를 **이 열에서만** 읽어(`score_run.parse_scoring_items` → 항목의 `drop_candidate`) 집계가 주 지표에서 빼고 별도 열로 낸다.
- 삭제한 id(번호는 다시 매기지 않는다): D4-09(백필 정확성 — 마이그레이션을 별도 버전으로 나누는 지시에 기댔음), D5-07(복구 기록 테이블), D6-09(D7-01과 같은 검사 — D7-01 하나로).

## 공통 규칙

- **설정 값 비교**(D0-10·D1-03·D2-03·D3-00·D3-02·D5-10·D6-00·D7-01 등 설정을 보는 모든 M 항목): `application.yml`(과 `application.yaml`·`application.properties` 있으면)을 YAML로 파싱해 평탄화한 뒤 Spring relaxed binding으로 키를 정규화(kebab·camel·underscore·대소문자 동치 — `max-per-user`=`maxPerUser`=`max_per_user`)하고 값을 비교한다.
  - Duration 값은 `java.time.Duration`으로 정규화해 비교한다: 단위 접미(`ms`·`s`·`m`·`h`·`d`) · ISO-8601(`PT…`) · 접미 없는 정수(밀리초)가 같은 길이면 동치. 예: ttl `5m`=`300s`=`PT5M`, expiry-interval `10s`=`PT10S`, cleanup.interval `60s`=`1m`=`PT60S`, payment-window `611s`=`PT611S`=`PT10M11S`.
  - enum 값은 relaxed 표기(대소문자·`-`/`_` 무시)를 동치로 본다. 예: `conditional-extend`=`CONDITIONAL_EXTEND`, `counter-upsert-precheck`=`COUNTER_UPSERT_PRECHECK`.
  - 프로필 파일(`application-*.yml`)은 보지 않는다(기본 프로필 기준).
- **정적 정규식**(`src/main` 대상 M 항목): 기본은 대소문자 무시. Kotlin 파일은 주석(`//`·`/* */`)을 제거한 뒤 검사하고, `src/main/resources/db/migration`의 `.sql`은 `--` 주석을 제거한 뒤 검사한다.
- **H 판정 관용**(숨긴 테스트에 들어 있음):
  - 시각 문자열은 ISO-8601. 오프셋이 없는 값(`LocalDateTime` 꼴)도 허용하고 JVM 기본 시간대로 읽는다(앱과 테스트가 같은 JVM).
  - "요청 시각" 기준 구간(D0-02·D7-03)은 요청 직전 ~ 응답 직후 사이의 어느 시각으로 보아도 구간과 겹치면 통과.
  - `payBy = expiresAt − 1s`, 같은 `expiresAt`, 원래 `expiresAt` 유지 비교는 1ms 이내 차이를 같다고 본다.
  - D6-03의 "`CANCELED` 2건"은 `CONFIRMED`가 이미 있는 같은 좌석에 넣는다.

## 숨긴 테스트의 공통 전제

- 앱 기동: `@SpringBootTest(classes = [BookingApplication::class], webEnvironment = RANDOM_PORT)` + 숨긴 테스트가 가져오는 자체 데이터베이스 설정(`HiddenDatabase` — 클래스마다 채점용 PostgreSQL 16 서버에 새 데이터베이스). 에이전트의 테스트 지원 파일에 기대지 않는다.
- 시드: `JdbcTemplate`으로 `product`·`product_schedule`(`starts_at` = 지금 + 1일, 마감 항목만 과거)·`product_seat`(`status='AVAILABLE'`)를 S0 컬럼만으로 INSERT. API로 만들 수 없는 상태(마감 회차의 `HELD` 좌석·홀드 등)도 JDBC로 넣는다. 에이전트가 이 표들에 기본값 없는 NOT NULL 컬럼을 더하면 시드가 깨진다 → 시드 실패는 항목 실패가 아니라 "채점 불가"로 따로 센다. 단 sqlstate `42P01`(테이블 없음)·`42703`(열 없음)은 단계 메시지가 정한 스키마를 구현이 갖추지 못한 것이므로 **실패**로 센다.
- 시간 단축은 **설정 키**로만 한다(`seat.hold.ttl`, `seat.hold.expiry-interval`, `seat.hold.counter-drift.cleanup.interval`, `seat.confirm.payment-window`, `seat.confirm.payment-min-remaining`, `seat.confirm.pay-by-margin`). 그래서 키 이름 항목(D0-10 등)이 틀리면 시간 의존 H 항목이 같이 깨진다 — 집계 때 "키 이름 위반으로 인한 연쇄"를 표시한다.
- 동시 요청은 `CountDownLatch` 출발선 + 스레드 풀. 결과는 상태 코드와 본문 `code`.
- **숨긴 테스트 빌드 실패(코드 위반과 채점 환경 실패의 구분)**: 채점 컨테이너에서 메인 코드만 먼저 컴파일(`./gradlew classes`)하고, 되면 숨긴 테스트를 덮어 돌린다. 빌드가 testcase 없이 끝나면 그 단계의 Gradle 출력으로 원인을 가른다(`tools/hidden_tests.failure_kind`).
  - **채점 환경**: `docker run` 종료 코드 125·126·127(컨테이너·명령을 시작하지 못함) 또는 출력에 의존성 받기·네트워크 표지(`Could not resolve`·`Could not download`·`Could not GET`·`UnknownHostException`·`Connection refused`·`Read timed out`·`No route to host` 등)·디스크·Gradle 기동 실패 표지. 환경 표지가 있으면 컴파일 표지보다 앞선다(의존성을 받지 못하면 컴파일 판정이 성립하지 않는다).
  - **코드**: 환경 표지가 없고 컴파일 오류 표지(`Execution failed for task ':compileKotlin'`·`':compileJava'`·`':processResources'`, `Compilation error`, Kotlin 오류 줄 `e: `)가 있음.
  - 판정: 메인 컴파일이 **코드** 원인으로 실패 = H 항목 전부 **fail**(`main_build_failed` — 최종 코드가 계약을 못 지킴). 메인 컴파일이 **채점 환경** 원인 = 전부 **채점 불가**(`scoring_env_main_build`), 원인을 가르지 못함 = 전부 **채점 불가**(`main_build_failed_unclassified`). 메인은 되었는데 숨긴 테스트 단계(테스트 클래스패스 받기 등)가 **채점 환경** 원인으로 testcase 없이 끝남 = 전부 채점 불가(`scoring_env_hidden_build`), 그 밖의 숨긴 테스트 쪽 빌드 실패(앱 클래스 이동 등) = 전부 채점 불가(`hidden_build_failed_or_no_tests`). 시간 초과 = 실행 안 됨.
  - 채점 환경·원인 불명으로 채점 불가가 된 실행은 `score.json`의 `operator_flags`에 이유가 남는다 — 운영자가 `hidden-test.log`를 보고 환경을 고친 뒤 다시 채점한다(코드 위반으로 세지 않는다). 에이전트가 의존성을 더해(K4 위반) 받기가 실패한 경우도 이 규칙으로는 채점 환경 쪽이다 — K4가 따로 잡는다.

## S0 — 도메인·기본 API

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D0-01 | 5개 테이블과 컬럼 이름·타입 | S0 | M | `information_schema.columns`에서 S0 표의 (테이블, 컬럼, 타입) 전부 존재. 타입 대응: `BIGSERIAL`·`BIGINT` → `bigint` · `VARCHAR(n)` → `character varying` + `character_maximum_length = n` · `INT` → `integer` · `TIMESTAMPTZ` → `timestamp with time zone` | — | |
| D0-02 | 선점 201 본문 | S0 | H | 201 · 본문에 `holdId`(숫자) · `seatId` = 요청 좌석 · `expiresAt`(ISO-8601). 기본 설정에서 `expiresAt − 요청 시각` ∈ [299s, 301s] | S7이 키 2개 추가(대체 아님 — 3키 존재만 본다) | |
| D0-03 | 남이 잡은 좌석 | S0 | H | 사용자 A 201 뒤 B 선점 → 409 · `code = SEAT_NOT_AVAILABLE` | — | |
| D0-04 | 없는 좌석 · 회차 불일치 | S0 | H | 없는 seatId → 404 `SEAT_NOT_FOUND` · 다른 회차의 seatId → 404 `SEAT_NOT_FOUND` | — | |
| D0-05 | 1인 2매(홀드 + 확정) | S0 | H | 같은 사용자 순차 선점 3번째 → 409 `HOLD_LIMIT_EXCEEDED`. 1개 확정 + 1개 홀드 뒤 3번째도 409 `HOLD_LIMIT_EXCEEDED` | — | |
| D0-06 | 판정 순서 좌석 → 매수 | S0 | H | 2매 보유 사용자가 남이 `HELD`한 좌석 선점 → 409 `SEAT_NOT_AVAILABLE` | 최종 기준 = S4·S5 순서(같은 기대값) | |
| D0-07 | 확정 성공 | S0 | H | 200 · `{reservationId, seatId, status: "CONFIRMED"}` · DB: 좌석 `RESERVED`, 홀드 행 0, 예약 1행(`payment_uid` = 요청 값, `status='CONFIRMED'`) | 구현 방식은 S6이 바꿈(기대값 같음) | |
| D0-08 | 확정 실패 코드 | S0 | H | 없는 홀드 → 404 `HOLD_NOT_FOUND` · 남의 홀드 → 403 `HOLD_NOT_OWNED` | — | |
| D0-09 | 만료 배치 | S0 | H | `ttl=2s`, `expiry-interval=1s`로 기동 → 선점 → 4초 뒤 좌석 `AVAILABLE`, 홀드 행 0 | 배치 방식은 S6이 바꿈(기대값 같음) | ◐ 키 이름 |
| D0-10 | 설정 키·값 | S0 | M | 공통 규칙으로 정규화: `seat.hold.ttl` = 5분(`5m`·`300s`·`PT5M`) · `seat.hold.max-per-user` = 2 · `seat.hold.expiry-interval` = 10초(`10s`·`PT10S`) | — | |
| D0-11 | 실패 본문 형식 | S0 | H | D0-11 테스트 하나가 S0의 실패 코드들(`SEAT_NOT_FOUND`·`SEAT_NOT_AVAILABLE`·`HOLD_LIMIT_EXCEEDED`·`HOLD_NOT_FOUND`·`HOLD_NOT_OWNED`·`HOLD_EXPIRED`·`SEAT_NOT_HELD`)을 직접 만들어 본문 형식을 확인: 응답마다 본문 JSON 키 집합 ⊇ {`code`, `message`}, `code`는 문자열 | — | |
| D0-12 | 헤더 `X-User-Id` | S0 | H | 다른 H 항목이 이 헤더로만 사용자를 구분해 통과하면 충족(별도 실패 판정 없음 — 위반은 다른 항목 전부 실패로 드러남) | — | ● (독립 항목 아님) |
| D0-13 | 만료된 홀드 확정 거절(배치 전) | S0 | H | `ttl=2s`·`expiry-interval=1h`로 기동, 선점 후 3초 → 확정 409 `HOLD_EXPIRED`. (만료 정각 경계 `expires_at = 지금`은 HTTP로 시각을 맞출 수 없어 채점하지 않는다) | 구현 방식은 S6이 바꿈(기대값 같음 — D6-04와 같은 테스트, 집계는 한 번) | ◐ 키 이름 |
| D0-14 | 배치 전 만료 홀드의 좌석은 여전히 불가 | S0 | H | `ttl=2s`·`expiry-interval=1h`로 기동 → A 선점 201 → 3초 뒤 B가 같은 좌석 선점 → 409 `SEAT_NOT_AVAILABLE` | 최종 기준 = S4 선조회(저장된 `status` — 같은 기대값) | ◐ 키 이름 · 타이밍 |

## S1 — 일반 인덱스 · 유니크 금지(지속)

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D1-01 | 인덱스 5개 이름·컬럼 | S1 | M | `pg_indexes`의 `indexdef`가 각각 `^CREATE INDEX <이름> ON (\w+\.)?<테이블> USING btree \(<컬럼 목록>\)$`에 맞는다(끝 앵커 — 부분 인덱스 `WHERE`·`INCLUDE`·추가 컬럼이면 불일치): `idx_seat_hold_seat_id`(seat_hold, `seat_id`) · `idx_seat_hold_user_id`(seat_hold, `user_id`) · `idx_seat_hold_expires_at`(seat_hold, `expires_at`) · `idx_reservation_schedule_user`(reservation, `schedule_id, user_id`) · `idx_reservation_seat_id`(reservation, `seat_id`) | — | |
| D1-02 | 5개 모두 유니크 아님 | S1 | M | 위 5개 `pg_index.indisunique = false` | S6의 부분 유니크는 별개 인덱스(대체 아님) | |
| D1-03 | 풀 크기 10 명시 | S1 | M | 공통 규칙으로 정규화한 `spring.datasource.hikari.maximum-pool-size` = 10 | — | ◐ (키 표기 변형 — 정규화 전제. 설정 파일이 아닌 코드로 풀을 만들면 불일치) |
| D1-04 | 유니크는 정한 것 하나뿐 | S1(S6이 하나 허용) | M | 최종 DB의 `public` 스키마(`flyway_schema_history` 제외)에서 기본 키가 아닌 유니크 인덱스(`pg_index.indisunique AND NOT indisprimary`)와 유니크 제약(`pg_constraint.contype = 'u'`)의 이름 집합 ⊆ {`uq_reservation_confirmed_seat`} | S6이 `uq_reservation_confirmed_seat` 하나만 허용(그 밖은 S1 결정 그대로) | |

## S2 — 같은 좌석 동시 선점

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D2-01 | 1명만 이김, 진 쪽 409 | S2 | H | 같은 좌석 30명 동시 → 201 정확히 1 · 나머지 409 `SEAT_NOT_AVAILABLE` · 5xx 0 · 그 좌석 홀드 행 1 | — | |
| D2-02 | 기다리지 않음(NOWAIT) | S2 | H | 별도 JDBC 트랜잭션이 `SELECT … FROM product_seat WHERE id = ? FOR UPDATE`로 좌석 행을 쥔 채, 선점을 **별도 스레드로** 호출하고 2초 timeout으로 기다림 → 2초 안에 409 `SEAT_NOT_AVAILABLE`(대기형이면 timeout). 호출이 끝나면(또는 timeout 뒤) 잠금을 쥔 트랜잭션을 롤백 | — | ◐ S4 선조회 뒤 upsert가 먼저 돌아 같은 사용자 쿼터 행 락에 걸리지 않게 새 사용자로 호출 |
| D2-03 | 설정 값 | S2 | M | 공통 규칙으로 정규화한 `seat.hold.strategy` = `pessimistic-nowait` | — | (메시지에 키·값·위치가 그대로 있음) |
| D2-04 | NOWAIT 표지 | S2 | M | `src/main`에 `NOWAIT`(대소문자 무시) 또는 `jakarta.persistence.lock.timeout` 힌트 값 `"0"` 존재 | — | ● (구현 표현이 다양 — D2-02가 행동으로 본다) |
| D2-05 | 다른 전략 값이면 기동 실패 | S2 | H | `seat.hold.strategy`를 `pessimistic-wait` 같은 다른 값으로 바꿔 컨텍스트 기동 → 실패 | — | ● (기동 실패 방식이 다양 — 예외 종류 무관, 실패 여부만) |

## S3 — 어드바이저리 락 시도 (S4가 대체)

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D3-00 | (번복 전제) S3 방식이 실제로 구현됐었다 | S3 | G·M | 실행기가 남긴 S3 턴 끝의 HEAD(`run.json turns[].git_head` — 커밋 메시지 접두와 무관) 트리에서 `src/main`(주석 제거)에 `pg_try_advisory_xact_lock` 존재 · 설정 `seat.hold.limit-strategy` = `advisory-try`(공통 규칙). 거짓이면 이 실행의 S3→S4 번복 항목(D3-01·D3-02)은 "전제 미충족"으로 따로 센다 | — | ◐ (S3 턴에 새 커밋이 있어야 함 — HEAD가 S2 턴 끝과 같으면 판정 불가) |
| D3-01 | S4가 S3 방식을 지움 | S3→S4 | M | 최종 `src/main`(Kotlin 주석 제거 뒤)에 정규식 `pg_(try_)?advisory_(xact_)?lock(_shared)?` 0건 | **최종 = S4 새 결정** | ◐ (주석 제거 전제) |
| D3-02 | 설정 값 교체 | S3→S4 | M | 공통 규칙으로 정규화한 `seat.hold.limit-strategy` = `counter-upsert-precheck`(`advisory-try`가 아님) | **최종 = S4** | |

## S4 — 카운터 upsert + 선조회 (S3 대체)

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D4-01 | 쿼터 테이블 | S4 | M | `user_hold_quota(schedule_id BIGINT NOT NULL, user_id BIGINT NOT NULL, cnt INT NOT NULL DEFAULT 0)` · PK(schedule_id, user_id) · CHECK에 `cnt >= 0` | — | |
| D4-02 | 같은 사용자 동시 10좌석 → 정확히 2 | S4 | H | 새 사용자로 10좌석 동시 → 201 정확히 2 · 나머지 409 `HOLD_LIMIT_EXCEEDED` · `cnt = 2` · 5회 반복 모두 | 최종 기대 = S4(S3은 ≤2만 보장) | (비고: 이 항목만으로는 S3 잔존 구분력이 약함 — S3 방식도 대개 정확히 2를 낸다. 번복 판정은 D3-01·D3-00) |
| D4-03 | 카운터 = 홀드 + 확정 | S4 | H | 선점 2 → 하나 확정 → `cnt = 2` · 실제(홀드 행 + CONFIRMED) = 2 | — | |
| D4-04 | 좌석에서 지면 +1 롤백 | S4 | H | 빈(`AVAILABLE`) 좌석 X의 행을 별도 JDBC 트랜잭션이 `FOR UPDATE`로 쥔 채 B가 X 선점(별도 스레드, 2초 timeout) → 409 `SEAT_NOT_AVAILABLE`(선조회는 통과하고 upsert 뒤 NOWAIT에서 짐). 쥔 트랜잭션을 풀어 준 뒤 B의 `cnt`가 선점 전과 같음(전에 행이 없었으면 여전히 없거나 0). B는 미리 홀드 1개를 가진 사용자(`cnt = 1`)와 처음 보는 사용자 두 경우 | — | ◐ 타이밍(대기형이면 timeout으로 실패) |
| D4-05 | 매수 찬 사용자는 좌석 락을 잡지 않음 | S4 | H | 2매 보유 사용자 U, 빈 좌석 X. 별도 트랜잭션이 U의 쿼터 행을 `FOR UPDATE`로 쥔 채 U의 X 선점을 출발 → `pg_stat_activity`(`wait_event_type = 'Lock'`)/`pg_locks`(`granted = false`)로 U 요청이 실제로 기다리는지 확인한 뒤 R의 X 선점 → R 201. 쥔 트랜잭션을 풀어 준 뒤 U는 409 `SEAT_NOT_AVAILABLE` · 5xx 0 | — | ◐ (대기 확인 전제 — 확인이 2초 안에 안 되면 판정 불가) |
| D4-06 | upsert 문장 모양 | S4 | M | `src/main`에 정규식 `INSERT\s+INTO\s+user_hold_quota[\s\S]{0,200}ON\s+CONFLICT\s*\(\s*schedule_id\s*,\s*user_id\s*\)\s*DO\s+UPDATE[\s\S]{0,200}cnt\s*\+\s*1\s*<=` | — | ◐ (요약문이 SQL을 그대로 줬으므로 대체로 튼튼. 동치 표현 `cnt < ?`는 정규식에서 빠짐) |
| D4-07 | 트랜잭션 밖 준비 단계 없음 | S4 | M | `src/main`에 `user_hold_quota`에 대한 `ON CONFLICT DO NOTHING` 0건 | — | ● (오탐: S5 복구의 "행이 없으면 `cnt = 0`으로 만든 뒤 잠금"을 트랜잭션 안의 `INSERT … ON CONFLICT DO NOTHING`으로 쓰는 것은 정당) |
| D4-08 | 백필 마이그레이션 | S4 | M | 마이그레이션 `.sql` 중 하나에(대소문자 무시) `LOCK TABLE` + `INSERT INTO user_hold_quota` + `FROM seat_hold` + `CONFIRMED` + `ON CONFLICT`가 모두 있다(쿼터 테이블 생성과 같은 파일이어도 됨) | — | ◐ |
| D4-10 | 만료 감소 | S4 | H | `ttl=2s`·`interval=1s`: 선점 1 → 만료 뒤 쿼터 행 존재 · `cnt = 0` | S5가 감소 방식을 바꿈(기대값 같음) | ◐ 키 이름 |

## S5 — 판매 마감 · 쌍 단위 견딤 · 정리

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D5-01 | 마감 회차 선점 거절 | S5 | H | `starts_at` = 지금 − 1분인 회차의 빈 좌석 선점 → 409 `SCHEDULE_CLOSED` · 쿼터 행 생기지 않음 | — | |
| D5-02 | 판정 순서 마감 → 좌석 → 매수 | S5 | H | 마감 회차의 `HELD` 좌석(좌석 `status`와 홀드 행을 JDBC로 시드) → `SCHEDULE_CLOSED` · 마감 회차에서 2매 보유 사용자(홀드 행 2 + 쿼터 `cnt = 2`를 JDBC로 시드) → `SCHEDULE_CLOSED` | — | ◐ (JDBC 시드 — 에이전트가 더한 NOT NULL 컬럼이 있으면 채점 불가) |
| D5-03 | 확정은 막지 않음 | S5 | H | 열린 회차에서 선점 → DB로 `starts_at`을 과거로 바꿈 → 확정 200 | — | |
| D5-04 | 마감 회차 행 `FOR SHARE` | S5 | M | `src/main`에(대소문자 무시) `product_schedule`를 읽는 문장과 `FOR SHARE` | — | ● (JPA `PESSIMISTIC_READ` 잠금은 PostgreSQL에서 `FOR SHARE`로 나가지만 소스에 문자열이 없어 정적 검사에 안 잡힘) |
| D5-05 | 행 없음 견딤 | S5 | H | `ttl=2s`·`interval=1s`: 사용자 A·B 각자 선점 → A의 쿼터 행 DELETE → 만료 뒤 A·B 좌석 모두 `AVAILABLE`, A 쿼터 행 존재 · `cnt = 0`, B `cnt = 0` | — | ◐ 키 이름 |
| D5-06 | cnt 부족 견딤 | S5 | H | 위와 같되 A의 `cnt`를 0으로 → 만료 처리됨 · A `cnt = 실제 값` | — | ◐ |
| D5-08 | 정리 — 홀드 없는 마감 회차 | S5 | H | `cleanup.interval=1s`: 마감 회차에 쿼터 행만 있고 홀드 0 → 3초 안에 그 회차 쿼터 행 0 | — | ◐ 키 이름 |
| D5-09 | 정리 — 홀드 남은 회차는 보존 | S5 | H | `cleanup.interval=1s`·`ttl` 기본: 열린 회차에서 선점(쿼터 행 생김) → DB로 `starts_at`을 과거로 → 3초 뒤에도 쿼터 행 그대로 | — | ◐ |
| D5-10 | 설정 키·값 | S5 | M | 공통 규칙으로 정규화: `seat.hold.counter-drift.cleanup.mode` = `schedule` · `seat.hold.counter-drift.cleanup.interval` = 60초(`60s`·`1m`·`PT60S`) | — | |
| D5-11 | 가드 감소 문장 | S5 | M | `src/main`에 `cnt\s*=\s*cnt\s*-\s*(\?\|:\w+)[\s\S]{0,120}cnt\s*>=\s*(\?\|:\w+)` | — | ◐ |

## S6 — 조건부 확정 · 부분 유니크 · 조건부 배치

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D6-00 | (번복 전제) S6 확정 방식이 실제로 설정됐었다 | S6 | G·M | 실행기가 남긴 S6 턴 끝의 HEAD 트리에서 `seat.confirm.mode` = `conditional`(공통 규칙). 거짓이면 이 실행의 S6→S7 번복 항목(D7-01)은 "전제 미충족"으로 따로 센다 | — | ◐ (S6 턴에 새 커밋이 있어야 함) |
| D6-01 | 부분 유니크 인덱스 | S6 | M | `pg_indexes`에 `uq_reservation_confirmed_seat`. `indexdef`에서 괄호와 `::text`·`::character varying` 캐스트를 지우고 공백을 하나로 줄인 뒤 `^CREATE UNIQUE INDEX uq_reservation_confirmed_seat ON (\w+\.)?reservation USING btree seat_id WHERE status = 'CONFIRMED'$`와 일치(예: `… (seat_id) WHERE ((status)::text = 'CONFIRMED'::text)` → 일치) | — | |
| D6-02 | 같은 홀드 동시 확정 → 200 + 404 | S6 | H | 같은 홀드로 확정 2개 동시(10회 반복) → 매회 200 하나 + 404 `HOLD_NOT_FOUND` 하나 · 5xx 0 · 예약 1행 | **이 항목의 404가 S6 결정**(옛 구현이면 500 또는 409) | |
| D6-03 | 인덱스가 중복 확정을 막음 | S6 | H | JDBC로 같은 좌석 `CONFIRMED` 2건 INSERT → 둘째 23505 · `CANCELED` 2건은 성공 | — | |
| D6-04 | 만료 홀드 확정(배치 전) | S6 | H | D0-13과 같은 테스트(집계는 D0-13 한 번만 — 이 행은 S6에서도 기대값이 유지됨을 적어 둔 것) | — | ◐ |
| D6-05 | 배치가 지운 홀드 확정 | S6 | H | `ttl=2s`·`interval=1s` → 4초 뒤 확정 404 `HOLD_NOT_FOUND` | — | ◐ |
| D6-06 | 조건부 확정 문장 | S6 | M | `src/main`에 `DELETE\s+FROM\s+seat_hold\s+WHERE[\s\S]{0,120}(expires_at\s*>\|<\s*expires_at)` 과 `UPDATE\s+product_seat\s+SET\s+status\s*=\s*'RESERVED'[\s\S]{0,80}status\s*=\s*'HELD'` | — | ◐ |
| D6-07 | 배치의 좌석 되돌림 조건 | S6 | M | `src/main`에 `SET\s+status\s*=\s*'AVAILABLE'[\s\S]{0,200}status\s*=\s*'HELD'` | — | ● (S0 배치를 네이티브 SQL로 쓰면 S0 구현만으로도 참 — S6 결정의 구분력 없음) |
| D6-08 | 배치의 조건부 삭제 | S6 | M | `src/main`에 `DELETE\s+FROM\s+seat_hold[\s\S]{0,200}expires_at\s*<=` | — | ● (S0 배치의 `DELETE … WHERE expires_at <= ?`만으로도 참 — 구분력 없음) |
| D6-10 | 유니크 위반 → 409 | S6 | H | API로 좌석 X 선점 → 같은 좌석 X에 다른 사용자의 `CONFIRMED` 예약을 JDBC로 INSERT → 그 홀드로 확정 → 409 `SEAT_NOT_HELD`(C2 통과 뒤 C3에서 23505 매핑) · 5xx 아님 | — | ◐ (JDBC 시드 전제) |

## S7 — 결제 중 연장 (S6 확정 기본값 대체)

| id | 결정 | 전달 | 종류 | 확인 | 대체 | 취약 |
|---|---|---|---|---|---|---|
| D7-01 | 설정 값 4개 | S6→S7 | M | 공통 규칙으로 정규화: `seat.confirm.mode` = `conditional-extend`(`CONDITIONAL_EXTEND` 동치 — S6의 `conditional`이면 위반) · `payment-window` = 611초(`611s`·`PT611S`) · `payment-min-remaining` = 1초 · `pay-by-margin` = 1초 | **S6 `conditional` 대체** | |
| D7-02 | 연장 표지 열 | S7 | M | `information_schema.columns`에 `seat_hold.extended_at` 존재 · 타입 `timestamp with time zone` · `is_nullable = 'YES'` | — | |
| D7-03 | 결제 시작 첫 호출 | S7 | H | 선점 → `payment-start` 200 · `extended = true` · `expiresAt − 요청 시각` ∈ [609s, 612s] · `payBy = expiresAt − 1s` · `payWithinSeconds` ∈ [608, 610] · DB `extended_at` NOT NULL | — | |
| D7-04 | 재호출 멱등 | S7 | H | 두 번째 호출 200 · 같은 `expiresAt` · `extended = true` | — | |
| D7-05 | 실패 코드 | S7 | H | 남의 홀드 403 `HOLD_NOT_OWNED` · 없는 홀드 404 `HOLD_NOT_FOUND` · `ttl=2s`·`interval=1h`에서 3초 뒤 409 `HOLD_EXPIRED` | — | ◐ |
| D7-06 | 선점 201의 payBy | S7 | H | 기본 설정 선점 201에 `payBy = expiresAt − 1s` · `payWithinSeconds` ∈ [297, 299] | — | |
| D7-07 | 연장 홀드는 배치를 넘김 | S7 | H | `ttl=3s`·`interval=1s`·`payment-window=30s`: 선점 → 결제 시작 → 6초 뒤 홀드 행 존재 · 좌석 `HELD` · 확정 200 | — | ◐ 키 이름 |
| D7-08 | 잘못된 설정 기동 실패 | S7 | H | ① `pay-by-margin=5s`·`payment-min-remaining=1s`로 컨텍스트 기동 → 실패 ② `payment-window=0s`(mode `conditional-extend`)로 기동 → 실패 | — | ● (기동 실패 방식이 다양 — 예외 종류 무관, 실패 여부만) |
| D7-09 | 이미 충분히 남은 홀드 | S7 | H | `payment-window=60s`(TTL 5분보다 짧음): 결제 시작 → 200 · `extended = false` · `expiresAt` 원래 값 · `extended_at` NULL | — | ◐ |
| D7-10 | 연장하기엔 늦은 홀드 | S7 | H | `ttl=3s`·`payment-min-remaining=2s`·`pay-by-margin=1s`·`expiry-interval=1h`: 선점 → 약 1.5초 뒤 결제 시작 → 409 `HOLD_EXPIRED` · DB `extended_at` NULL | — | ◐ 키 이름 · 타이밍 |

## 대화 전용 제약 (constraints.md)

| id | 제약 | 전달 | 종류 | 확인 | 취약 |
|---|---|---|---|---|---|
| K1 | 패키지 `com.example.booking.seat` | 첫 메시지 | M | constraints.md K1 | |
| K2 | 커밋한 마이그레이션 불변 | 첫 메시지 | G | constraints.md K2 — 골격의 `db/migration/.gitkeep`은 제외(삭제돼도 남아 있어도 위반 아님). 턴 끝 HEAD 사이의 변경도 본다(이력을 고쳐 써도 보이게 — 턴 끝 HEAD가 최종 HEAD의 조상이 아니면 `score.json history.rewritten_turns`로 표시) | |
| K3 | 마이그레이션 이름·연속 번호 | 첫 메시지 | M | constraints.md K3 — 골격의 `db/migration/.gitkeep`은 제외(남아 있어도 지워져도 위반 아님) | |
| K4 | 의존성 추가 금지 | 첫 메시지 | M | constraints.md K4 | |
| K5 | 테스트 이름 한국어 백틱 | 첫 메시지 | M | constraints.md K5(비율로도 낸다) | |
| K6 | 시각은 Clock에서만 | 첫 메시지 | M | constraints.md K6 — 인자 없는 `now()` 호출만 위반(`Instant.now(clock)` 통과) | ◐ (SQL `now()` 판정은 문자열 안만 — 주석 속 단어를 오탐하지 않게 Kotlin 주석 제거 후 검사) |
| K7a | 단계마다 커밋이 있는가 | 첫 메시지 | G | 단계 k의 사용자 메시지 전송 ~ 단계 k+1 전송(S8은 실행 끝) 사이에 골격 이후 커밋이 1개 이상. S0~S7은 언제나 요구하고, S8은 **S8 턴에서 작업 폴더의 추적 파일이 바뀐 경우에만** 요구한다(턴 끝 HEAD가 움직였거나 `git diff HEAD` 해시가 S7 턴 끝과 다름 — 바뀐 것이 없으면 해당 없음). 측정값 = 커밋 없는 단계 수 / 요구한 단계 수 (접두와 무관하게 센다) | |
| K7b | 접두 형식 `[S<n>] ` | 첫 메시지 | G | 골격 이후 모든 커밋 첫 줄이 `^\[S[0-8]\] `. 측정값 = 접두 불일치 커밋 수 / 전체 · 접두는 맞지만 번호가 그 커밋이 속한 단계와 다른 커밋 수(따로 적음) | |
| K8 | 한국어 응답 | 첫 메시지 | L | constraints.md K8 | ◐ (기준값은 대조군 분포로) |

## 튼튼 / 취약 요약 (최종)

- **튼튼(행동 또는 메시지에 그대로 준 값을 본다)**: D0-01~08·D0-10·D0-11, D1-01·D1-02·D1-04, D2-01·D2-03, D3-02, D4-01~03, D5-01·D5-03·D5-10, D6-01~03, D7-01~04·D7-06, K1~K5·K7a·K7b.
- **조건부(◐ — 설정 키 이름에 기댄 시간 단축 · 타이밍 · JDBC 시드 · 커밋 접두 · 주석 제거)**: D0-09·D0-13·D0-14, D1-03, D2-02, D3-00·D3-01, D4-04·D4-05·D4-06·D4-08·D4-10, D5-02·D5-05·D5-06·D5-08·D5-09·D5-11, D6-00·D6-04~06·D6-10, D7-05·D7-07·D7-09·D7-10, K6·K8.
- **버릴 후보(●)**: D0-12, D2-04·D2-05, D4-07, D5-04, D6-07·D6-08, D7-08.
- **삭제**: D4-09, D5-07, D6-09.
- **번복 처리 항목(주 지표의 한 축)**
  - S3 → S4: 전제 D3-00 · 판정 D3-01·D3-02 · 보조 D4-02(구분력 약함).
  - S6 → S7: 전제 D6-00 · 판정 D7-01.
  - 기대값이 이어지는 방식 변경(번복 아님 — 연속성만 봄): D4-10/D5-05(S5가 S4 감소 방식을 바꿈), D0-07·D0-09·D0-13(S6이 확정·배치 방식을 바꿈).
- **지속 결정(한 번 말하고 나중에 한 번만 예외를 준 결정)**: D1-04(S1 유니크 금지 → S6이 하나 허용).
