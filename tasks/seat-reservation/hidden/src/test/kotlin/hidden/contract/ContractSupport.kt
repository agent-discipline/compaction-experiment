package hidden.contract

import com.example.booking.BookingApplication
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.opentest4j.TestAbortedException
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.boot.test.context.TestConfiguration
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.boot.testcontainers.service.connection.ServiceConnection
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Import
import org.springframework.dao.DataAccessException
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.datasource.SimpleDriverDataSource
import org.springframework.test.annotation.DirtiesContext
import org.testcontainers.containers.PostgreSQLContainer
import org.testcontainers.utility.DockerImageName
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.sql.Connection
import java.sql.DriverManager
import java.time.Duration
import java.time.Instant
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.time.format.DateTimeParseException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * Hidden contract tests: the implementation under test is a black box.
 * Only HTTP (header X-User-Id) and direct JDBC against the test database are used.
 * Each test class = one Spring context with its own PostgreSQL container (closed after the class).
 */
@TestConfiguration(proxyBeanMethods = false)
class HiddenPostgresConfiguration {
    @Bean
    @ServiceConnection
    fun hiddenPostgresContainer(): PostgreSQLContainer<*> =
        PostgreSQLContainer(DockerImageName.parse("postgres:16"))
}

@SpringBootTest(
    classes = [BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
)
@Import(HiddenPostgresConfiguration::class)
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
abstract class ContractTestBase {

    @LocalServerPort
    var port: Int = 0

    @Autowired
    lateinit var postgres: PostgreSQLContainer<*>

    protected val jdbc: JdbcTemplate by lazy {
        JdbcTemplate(SimpleDriverDataSource(org.postgresql.Driver(), postgres.jdbcUrl, postgres.username, postgres.password))
    }

    protected fun rawConnection(): Connection =
        DriverManager.getConnection(postgres.jdbcUrl, postgres.username, postgres.password)

    // ---------------------------------------------------------------- HTTP

    data class Resp(val status: Int, val body: String) {
        val json: JsonNode? by lazy {
            try {
                if (body.isBlank()) null else MAPPER.readTree(body)
            } catch (e: Exception) {
                null
            }
        }
        val code: String? get() = json?.get("code")?.takeIf { it.isTextual }?.asText()
        override fun toString(): String = "HTTP $status $body"
    }

    private fun send(method: String, path: String, userId: Long?, jsonBody: String?): Resp {
        val b = HttpRequest.newBuilder(URI.create("http://localhost:$port$path"))
            .timeout(Duration.ofSeconds(30))
        if (userId != null) b.header("X-User-Id", userId.toString())
        if (jsonBody != null) {
            b.header("Content-Type", "application/json")
            b.method(method, HttpRequest.BodyPublishers.ofString(jsonBody))
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody())
        }
        val r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString())
        return Resp(r.statusCode(), r.body() ?: "")
    }

    protected fun hold(userId: Long, scheduleId: Long, seatId: Long): Resp =
        send("POST", "/api/schedules/$scheduleId/seats/$seatId/hold", userId, null)

    protected fun confirm(userId: Long, holdId: Long, paymentUid: String = "pay-${PAY.incrementAndGet()}"): Resp =
        send("POST", "/api/holds/$holdId/confirm", userId, MAPPER.writeValueAsString(mapOf("paymentUid" to paymentUid)))

    protected fun paymentStart(userId: Long, holdId: Long): Resp =
        send("POST", "/api/holds/$holdId/payment-start", userId, null)

    /** Hold that must succeed (precondition). A failure here fails the calling test. */
    protected fun mustHold(userId: Long, scheduleId: Long, seatId: Long): Long {
        val r = hold(userId, scheduleId, seatId)
        check(r.status == 201) { "precondition: hold(user=$userId, seat=$seatId) expected 201 but was $r" }
        val id = r.json?.get("holdId")
        check(id != null && id.isNumber) { "precondition: hold response has no numeric holdId: $r" }
        return id.asLong()
    }

    protected fun assertError(r: Resp, status: Int, code: String, label: String = "") {
        if (r.status != status || r.code != code) {
            throw AssertionError("$label expected $status $code but was $r")
        }
    }

    // ---------------------------------------------------------------- seeds (JDBC, S0 columns only)

    data class Sched(val id: Long, val seats: List<Long>)

    protected fun seed(block: () -> Unit) {
        try {
            block()
        } catch (e: DataAccessException) {
            val state = generateSequence<Throwable>(e) { it.cause }.filterIsInstance<java.sql.SQLException>().firstOrNull()?.sqlState
            val detail = "[sqlstate=$state]: ${e.mostSpecificCause.message}"
            // Missing table/column = the schema defined by the step messages is absent: a failure, not unscorable.
            if (state == "42P01" || state == "42703") throw AssertionError("SEED_SCHEMA_MISSING $detail", e)
            throw TestAbortedException("UNSCORABLE seed failed $detail", e)
        }
    }

    protected fun seedSchedule(seatCount: Int, startsAt: Instant = Instant.now().plus(Duration.ofDays(1))): Sched {
        var result: Sched? = null
        seed {
            val productId = jdbc.queryForObject(
                "INSERT INTO product (name) VALUES (?) RETURNING id", Long::class.java, "hidden-${SEQ.incrementAndGet()}",
            )!!
            val scheduleId = jdbc.queryForObject(
                "INSERT INTO product_schedule (product_id, starts_at) VALUES (?, ?) RETURNING id",
                Long::class.java, productId, ts(startsAt),
            )!!
            val seats = (1..seatCount).map { n ->
                jdbc.queryForObject(
                    "INSERT INTO product_seat (schedule_id, section, row_no, seat_no, status) VALUES (?, 'A', 1, ?, 'AVAILABLE') RETURNING id",
                    Long::class.java, scheduleId, n,
                )!!
            }
            result = Sched(scheduleId, seats)
        }
        return result!!
    }

    /** Seeds a live hold row (and marks the seat HELD) directly in the DB. */
    protected fun seedHold(scheduleId: Long, seatId: Long, userId: Long, expiresAt: Instant = Instant.now().plusSeconds(300)): Long {
        var id = 0L
        seed {
            jdbc.update("UPDATE product_seat SET status = 'HELD' WHERE id = ?", seatId)
            id = jdbc.queryForObject(
                "INSERT INTO seat_hold (seat_id, schedule_id, user_id, held_at, expires_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                Long::class.java, seatId, scheduleId, userId, ts(Instant.now()), ts(expiresAt),
            )!!
        }
        return id
    }

    protected fun seedQuota(scheduleId: Long, userId: Long, cnt: Int) = seed {
        jdbc.update("INSERT INTO user_hold_quota (schedule_id, user_id, cnt) VALUES (?, ?, ?)", scheduleId, userId, cnt)
    }

    protected fun ts(i: Instant): OffsetDateTime = OffsetDateTime.ofInstant(i, ZoneOffset.UTC)

    // ---------------------------------------------------------------- DB reads

    protected fun seatStatus(seatId: Long): String? =
        jdbc.queryForList("SELECT status FROM product_seat WHERE id = ?", String::class.java, seatId).firstOrNull()

    protected fun holdRowsForSeat(seatId: Long): Int =
        jdbc.queryForObject("SELECT count(*) FROM seat_hold WHERE seat_id = ?", Int::class.java, seatId)!!

    protected fun holdExists(holdId: Long): Boolean =
        jdbc.queryForObject("SELECT count(*) FROM seat_hold WHERE id = ?", Int::class.java, holdId)!! > 0

    /** null = no quota row. */
    protected fun quotaCnt(scheduleId: Long, userId: Long): Int? =
        jdbc.queryForList(
            "SELECT cnt FROM user_hold_quota WHERE schedule_id = ? AND user_id = ?", Int::class.java, scheduleId, userId,
        ).firstOrNull()

    protected fun quotaRowsForSchedule(scheduleId: Long): Int =
        jdbc.queryForObject("SELECT count(*) FROM user_hold_quota WHERE schedule_id = ?", Int::class.java, scheduleId)!!

    // ---------------------------------------------------------------- time / concurrency

    protected fun eventually(timeout: Duration, condition: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + timeout.toNanos()
        while (true) {
            if (condition()) return true
            if (System.nanoTime() >= deadline) return false
            Thread.sleep(100)
        }
    }

    /** Runs n calls released together from a start latch. A call that does not finish within the timeout yields status -1. */
    protected fun concurrently(n: Int, timeoutSeconds: Long = 30, call: (Int) -> Resp): List<Resp> {
        val pool = Executors.newFixedThreadPool(n)
        try {
            val ready = CountDownLatch(n)
            val start = CountDownLatch(1)
            val futures = (0 until n).map { i ->
                pool.submit<Resp> {
                    ready.countDown()
                    start.await()
                    call(i)
                }
            }
            ready.await(10, TimeUnit.SECONDS)
            start.countDown()
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds)
            return futures.map { f ->
                try {
                    f.get(maxOf(0, deadline - System.nanoTime()), TimeUnit.NANOSECONDS)
                } catch (e: java.util.concurrent.TimeoutException) {
                    Resp(-1, "TIMEOUT")
                } catch (e: java.util.concurrent.ExecutionException) {
                    Resp(-2, "CLIENT ERROR ${e.cause}")
                }
            }
        } finally {
            pool.shutdownNow()
        }
    }

    companion object {
        val MAPPER = ObjectMapper()
        val HTTP: HttpClient = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(10))
            .build()
        private val USERS = AtomicLong(1000)
        private val SEQ = AtomicLong()
        private val PAY = AtomicLong()

        fun newUser(): Long = USERS.incrementAndGet()

        /**
         * ISO-8601 instant. A value without offset is read in the JVM default zone
         * (the application runs in the same JVM).
         */
        fun parseTime(node: JsonNode?): Instant? {
            if (node == null || !node.isTextual) return null
            val s = node.asText()
            return try {
                OffsetDateTime.parse(s).toInstant()
            } catch (e: DateTimeParseException) {
                try {
                    ZonedDateTime.parse(s).toInstant()
                } catch (e2: DateTimeParseException) {
                    try {
                        LocalDateTime.parse(s).atZone(ZoneId.systemDefault()).toInstant()
                    } catch (e3: DateTimeParseException) {
                        null
                    }
                }
            }
        }

        /** True when [value - before, value - after] (the possible offsets from the request instant) meets [lo, hi] seconds. */
        fun offsetWithin(value: Instant, before: Instant, after: Instant, loSeconds: Long, hiSeconds: Long): Boolean {
            val maxOffsetMs = Duration.between(before, value).toMillis()
            val minOffsetMs = Duration.between(after, value).toMillis()
            return maxOffsetMs >= loSeconds * 1000 && minOffsetMs <= hiSeconds * 1000
        }
    }
}
