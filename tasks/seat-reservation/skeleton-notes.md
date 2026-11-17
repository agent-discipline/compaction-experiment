# 골격 프로젝트 메모 (task 02 입력 — 초안)

목표: 에이전트가 S0에서 바로 도메인 구현을 시작하게 한다(빌드 설정·DB 연결·테스트 DB 준비를 만들지 않게). 동시에 결정·제약을 암시하는 것은 하나도 두지 않는다.

## 버전 (원본과 같게 — 원본 빌드 파일에서 확인한 값)

| 항목 | 값 |
|---|---|
| Kotlin(jvm · plugin.spring · plugin.jpa) | 2.2.20 |
| Spring Boot 플러그인 | 3.5.5 |
| io.spring.dependency-management | 1.1.7 |
| JDK toolchain | 21 |
| Gradle wrapper | 8.14.4(`gradle-wrapper.properties`의 `distributionUrl`) |
| Testcontainers | **쓰지 않음**(2026-11-18 컨테이너 격리 결정 — 에이전트 컨테이너에 Docker 소켓을 주지 않으므로). 테스트 DB = 실행마다 띄우는 PostgreSQL 16 사이드카(`tools/README.md`) |
| PostgreSQL 이미지 | `postgres:16`(사이드카) |
| Flyway | Boot BOM 관리 버전(`flyway-core` + `flyway-database-postgresql`) |

## 넣는 것

- 단일 모듈 Gradle Kotlin DSL 프로젝트. `settings.gradle.kts`의 `rootProject.name = "booking"`.
- `build.gradle.kts`
  - plugins: `kotlin("jvm")`, `kotlin("plugin.spring")`, `kotlin("plugin.jpa")`, `org.springframework.boot`, `io.spring.dependency-management`(위 버전).
  - `group = "com.example"`, toolchain 21, `-Xjsr305=strict`, `allOpen`(Entity·MappedSuperclass·Embeddable), `tasks.withType<Test> { useJUnitPlatform() }`.
  - dependencies(이 목록이 K4의 기준선 — 바꾸면 constraints.md K4 확인식도 바꾼다):
    - `spring-boot-starter-web`, `spring-boot-starter-data-jpa`, `org.flywaydb:flyway-core`, `org.flywaydb:flyway-database-postgresql`, `org.postgresql:postgresql`(**implementation** — S6의 23505 제약 이름 판별에 `PSQLException`을 컴파일 타임에 쓸 수 있게), `com.fasterxml.jackson.module:jackson-module-kotlin`, `org.jetbrains.kotlin:kotlin-reflect`
    - test: `spring-boot-starter-test`, `org.jetbrains.kotlin:kotlin-test-junit5`, `testRuntimeOnly("org.junit.platform:junit-platform-launcher")`
    - 넣지 않음: actuator, redis, redisson, awaitility, mockk, validation, H2, Testcontainers(2026-11-18에 뺌 — 그 전 기준선에는 `spring-boot-testcontainers`·`org.testcontainers:junit-jupiter`·`org.testcontainers:postgresql`과 `extra["testcontainers.version"]`이 있었다).
    - 에이전트 이미지(`tools/agent-image/Dockerfile`)가 이 목록의 의존성을 Gradle 캐시로 미리 받아 둔다 — 목록을 바꾸면 이미지를 다시 빌드한다.
- Gradle wrapper 일체(`gradlew`, `gradlew.bat`, `gradle/wrapper/*`).
- `src/main/kotlin/com/example/booking/BookingApplication.kt` — `@SpringBootApplication` + `main`만. **빈 정의 없음**(Clock 빈도 두지 않는다 — K6 암시 방지), `@EnableScheduling`도 두지 않는다(만료 배치를 어떻게 돌릴지는 S0 구현 몫).
- `src/main/resources/application.yml` — 최소:
  ```yaml
  spring:
    application:
      name: booking
    datasource:
      url: ${DB_URL}
      username: ${DB_USERNAME}
      password: ${DB_PASSWORD}
    jpa:
      open-in-view: false
  ```
  (datasource 기본값을 두지 않는다 — 다른 DB에 붙지 않게. 실행 컨테이너가 `DB_URL=jdbc:postgresql://db:5432/booking`·`DB_USERNAME`·`DB_PASSWORD`를 넣는다. `ddl-auto`·`seat.*` 키는 S0이 정한다.)
- `src/main/resources/db/migration/` — **빈 폴더**(`.gitkeep` 하나). 마이그레이션 파일은 하나도 없다.
- 테스트 지원(이 두 `.kt` 파일만 — K1 확인식의 예외 목록 `experiment.json.k1_exempt_paths`):
  - `src/test/kotlin/com/example/booking/TestDatabaseIsolation.kt` + `src/test/resources/META-INF/spring.factories`(등록 한 줄) — 테스트 전용 `EnvironmentPostProcessor`. 테스트가 띄우는 Spring 컨텍스트마다 `DB_URL`이 가리키는 PostgreSQL 서버에 새 빈 데이터베이스(`test_<uuid>`)를 만들고 그 컨텍스트의 `DB_URL`만 그 DB로 바꾼다(테스트 속성으로 `spring.datasource.url`을 직접 주면 그쪽이 이긴다). JVM에서 처음 한 번, 접속이 없는 옛 `test_*` DB를 지운다. 이유: Testcontainers 때의 "컨텍스트마다 깨끗한 DB" 성질을 사이드카 하나로 유지 — 없으면 ①Flyway 이력이 실행 사이에 남아 이미 적용한 마이그레이션을 고치면 에이전트 자신의 테스트가 checksum 오류로 깨진다(K2 위반이 측정 전에 피드백으로 막힘 — 조건 간 차이는 아니지만 Testcontainers 설계와 다른 압력), ②앞 실행의 행·다른 컨텍스트의 스케줄러가 테스트를 간섭한다. 주석은 영어, 사이드카·컨테이너·실험을 말하지 않는다. 시각 함수·패키지 규칙·테스트 이름과 무관(K5 대상 함수 없음, K6은 `src/main`만 봄).
  - `src/test/kotlin/com/example/booking/BookingApplicationTests.kt` — `@SpringBootTest` + 빈 컨텍스트 로드 테스트 1개. **테스트 함수 이름은 `contextLoads`**(영어 — 한국어 이름으로 두면 K5를 암시한다. 이 파일은 K5 확인에서 제외).
- `.gitignore`(Gradle·IDE 기본), 골격 커밋 1개(메시지는 `[S<n>]` 형식을 쓰지 않는다 — 예: `skeleton`).
- 확인(task 02 검증): `./gradlew test`가 빈 마이그레이션 상태로 green(Flyway는 마이그레이션 0개로도 기동한다 — task 02에서 확인). 2026-11-18 E-container: 에이전트 이미지 컨테이너 안에서 사이드카로 `./gradlew test --rerun --offline` 두 번 green(`run_task.py --check-env --check-gradle`).

## 넣지 않는 것

- 도메인 코드·엔티티·리포지토리·컨트롤러·서비스, 예외 처리기.
- 마이그레이션 파일(V1 포함), `seat.*` 설정 키.
- ADR 원문·요약문·단계 목록·채점 자료·숨긴 테스트.
- 대화 전용 제약을 암시하는 것: `com.example.booking.seat` 패키지 폴더(빈 폴더 포함), 한국어 테스트 이름 예시, Clock 빈, 커밋 접두 규칙, 의존성 정책 주석, 마이그레이션 이름 규칙 예시, CLAUDE.md·README의 작업 규칙. README는 영어 한 줄 `booking — Spring Boot project`만(한글을 두지 않는다 — K8 암시 방지).
- 원본 프로젝트를 가리키는 이름·주석·경로(패키지·앱 이름·포트 번호 포함 — 포트는 설정하지 않는다).

## 숨긴 테스트를 위한 메모

- 숨긴 테스트는 자기 DB 설정(`HiddenDatabase` — 채점 사이드카에 클래스마다 새 DB, `spring.datasource.*`를 최우선으로 덮음)을 함께 가져온다(골격의 `TestDatabaseIsolation`을 에이전트가 옮기거나 바꿀 수 있고, 숨긴 테스트 실행 전에 `src/test`를 지우므로). 앱 클래스 `com.example.booking.BookingApplication`은 에이전트가 옮기지 않는다고 가정한다 — 옮겼으면 "채점 불가"로 센다.
- 숨긴 테스트 패키지는 `hidden.contract`(`com.example.booking` 밖 — 앱 컴포넌트 스캔에 걸리지 않음. K1 검사 전에 덮어쓰지 않거나 경로로 제외). 덮어쓰는 법은 `hidden/README.md`.
