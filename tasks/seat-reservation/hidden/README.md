# 숨긴 계약 테스트 (채점용 — 에이전트 작업 폴더에 두지 않는다)

`scoring-items.md`의 종류 H 항목을 HTTP(`X-User-Id`)와 JDBC로만 확인하는 JUnit 5 테스트. 에이전트 코드에서 가정하는 것은 `com.example.booking.BookingApplication` 하나뿐이다(옮겼으면 컴파일 실패 → 실행 전체 "채점 불가").

## 구성

- `src/test/kotlin/hidden/contract/` — 패키지 `hidden.contract`(`com.example.booking` 밖이라 앱의 컴포넌트 스캔에 걸리지 않음).
  - `ContractSupport.kt` — 공통 기반(`@SpringBootTest(RANDOM_PORT)` + `@DirtiesContext(AFTER_CLASS)`), HTTP·시드·DB 조회·동시 실행 도우미. 시드·조회용 JDBC는 앱과 같은 데이터베이스(`spring.datasource.*` 값)에 따로 붙는다.
  - `HiddenDatabase.kt` + `src/test/resources/META-INF/spring.factories` — `EnvironmentPostProcessor`. 이 JVM에서 뜨는 Spring 애플리케이션마다 채점용 PostgreSQL 서버(프로세스 환경 변수 `DB_URL`·`DB_USERNAME`·`DB_PASSWORD` — 채점 컨테이너가 넣는다)에 `CREATE DATABASE hidden_<uuid>`로 새 빈 데이터베이스를 만들고, `spring.datasource.url/username/password`(와 `DB_URL` 등)를 가장 높은 우선순위로 그 데이터베이스로 바꾼다(전의 `@ServiceConnection`과 같은 효과 — 앱 설정이 무엇이든 깨끗한 자기 DB). 환경 변수가 없으면 기동 실패(조용히 다른 DB에 붙지 않음).
  - 테스트 클래스 하나 = 설정 묶음 하나 = Spring 컨텍스트 하나 = 데이터베이스 하나(클래스가 끝나면 컨텍스트가 닫힘, 데이터베이스는 채점 사이드카와 함께 버림). 묶음별 설정 키는 `items.json`의 `contexts`.
  - `StartupValidationContractTest` — 앱을 직접 기동해 잘못된 설정이면 기동이 실패하는지 본다(D2-05·D7-08). 먼저 앱 자체 설정으로 대조 기동이 성공해야 한다. 기동마다 `HiddenDatabase`가 새 데이터베이스를 준다.
- `items.json` — 항목 id → 클래스·메서드·`@DisplayName`, 전달 단계, 취약 표시.

## 덮어 돌리는 법 (실행 끝난 최종 저장소 사본에서)

정적 검사(K1·K5 등)는 **덮어쓰기 전에** 끝낸다. 그다음(호스트에서 사본을 만들고, 테스트는 채점 컨테이너에서 — `tools/score_run.py`가 한다):

```bash
cd <최종 저장소 사본>
rm -rf src/test                         # 에이전트 테스트 전부 제거(컴파일 실패·test 리소스의 설정 덮어쓰기·테스트용 빈 간섭 차단)
cp -r <이 폴더>/src .                    # → src/test/kotlin/hidden/contract/*.kt, src/test/resources/META-INF/spring.factories
# 채점 컨테이너(에이전트 이미지 cx-agent, 토큰 없음) + PostgreSQL 16 사이드카(같은 Docker 네트워크, 별칭 db):
docker run --rm --user <uid>:<gid> --network <채점 네트워크> --mount type=bind,src=<사본>,dst=/work -w /work \
  -e DB_URL=jdbc:postgresql://db:5432/booking -e DB_USERNAME=booking -e DB_PASSWORD=booking \
  cx-agent:2.1.326 ./gradlew test --tests 'hidden.contract.*' --rerun --no-daemon --console=plain
```

- 숨긴 테스트를 덮기 전에 같은 채점 컨테이너에서 메인 코드만 먼저 컴파일한다(`./gradlew classes`, `experiment.json` `hidden.main_build_command`).
  메인이 컴파일되지 않으면 H 항목은 전부 **실패**(`main_build_failed`)이고, 메인은 되는데 숨긴 테스트만 컴파일되지 않을 때(앱 클래스 이동 등)만 "채점 불가"다.
- 에이전트 테스트를 지우는 이유: 에이전트의 테스트 코드가 컴파일되지 않으면 숨긴 테스트도 못 돌고, `src/test/resources/application.yml`이 있으면 메인 설정을 가린다. 에이전트 테스트는 채점하지 않는다.
- `build.gradle.kts`는 건드리지 않는다(골격 의존성만으로 컴파일됨 — Testcontainers 없음). 필요한 것: 에이전트 이미지(Gradle 캐시 포함)와 `postgres:16` 사이드카. 숨긴 테스트는 Docker를 쓰지 않는다.
- 종료 코드는 실패 항목이 있으면 1이다. 판정은 종료 코드가 아니라 XML로 한다.
- 걸리는 시간: 컨텍스트 7개 + 직접 기동 최대 4회(골격 기준 측정은 보고 참조).

## 결과 읽기

- 위치: `build/test-results/test/TEST-hidden.contract.<클래스>.xml`.
- `<testcase classname="hidden.contract.<클래스>" name="<@DisplayName>">` — `name`은 항상 `<항목 id> <설명>`으로 시작한다. `items.json`의 `class`·`display_name`으로 대응시킨다.
- 판정:
  - 통과 = 자식 요소 `failure`·`error`·`skipped` 없음.
  - 실패 = `failure` 또는 `error`. 컨텍스트 기동 실패면 그 클래스의 항목이 전부 같은 오류로 실패한다(메시지로 구분 가능).
  - 시드 실패 중 sqlstate `42P01`(테이블 없음)·`42703`(열 없음)은 **실패**다 — 메시지 `SEED_SCHEMA_MISSING [sqlstate=…]`인 `failure`(단계 메시지가 정한 스키마를 구현이 갖추지 못함).
  - 채점 불가 = `skipped`이고 메시지가 `UNSCORABLE`로 시작. 그 밖의 시드 실패는 `UNSCORABLE seed failed [sqlstate=XXXXX]: …`(예: `23502` — 에이전트가 기본값 없는 NOT NULL 열을 더함). D4-05는 대기를 2초 안에 확인하지 못하면 `UNSCORABLE`.
- `D6-04`는 `D0-13`과 같은 테스트(집계 한 번). `D0-12`는 테스트 없음(별도 판정 없음 — `items.json`의 `no_test`).
- 설정 키 이름이 틀리면 시간 단축 항목(◐ 키 이름)이 함께 깨진다 — 집계 때 D0-10·D5-10·D7-01과 함께 보고 "키 이름 연쇄"로 표시.

## 해석 규칙(테스트에 들어 있는 선택)

- 시각 문자열: ISO-8601. 오프셋이 있으면 그대로, 없으면(`LocalDateTime` 꼴) JVM 기본 시간대로 읽는다(앱이 같은 JVM에서 돈다).
- "요청 시각" 기준 구간(D0-02 [299,301]s, D7-03 [609,612]s): 요청 직전~응답 직후 사이의 어느 시각으로 보아도 구간과 겹치면 통과.
- `payBy = expiresAt − 1s`, 재호출 같은 `expiresAt`, 원래 `expiresAt` 유지: 1ms 이내 차이는 같다고 본다.
- 실패 응답의 코드 판정은 상태 코드 + 본문 `code`만 본다. `{code, message}` 형식은 D0-11 하나가 따로 본다(항목끼리 독립).
