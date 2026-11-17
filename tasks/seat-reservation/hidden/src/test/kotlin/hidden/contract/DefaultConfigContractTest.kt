package hidden.contract

import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import org.opentest4j.TestAbortedException
import java.sql.SQLException
import java.time.Duration
import java.time.Instant
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue
import kotlin.test.fail

/** Items checked with the application's own configuration (no property overrides). */
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class DefaultConfigContractTest : ContractTestBase() {

    // ------------------------------------------------------------------ S0

    @Test
    @DisplayName("D0-02 hold 201 body and default TTL")
    fun D0_02_holdCreatedBody() {
        val s = seedSchedule(1)
        val u = newUser()
        val before = Instant.now()
        val r = hold(u, s.id, s.seats[0])
        val after = Instant.now()
        assertEquals(201, r.status, "status: $r")
        val json = assertNotNull(r.json, "body: $r")
        assertTrue(json.get("holdId")?.isNumber == true, "holdId must be a JSON number: $r")
        assertTrue(json.get("seatId")?.isNumber == true && json.get("seatId").asLong() == s.seats[0], "seatId: $r")
        val expiresAt = assertNotNull(parseTime(json.get("expiresAt")), "expiresAt must be an ISO-8601 string: $r")
        assertTrue(offsetWithin(expiresAt, before, after, 299, 301), "expiresAt - request time not in [299s, 301s]: $r (request $before..$after)")
    }

    @Test
    @DisplayName("D0-03 seat held by another user")
    fun D0_03_seatHeldByAnother() {
        val s = seedSchedule(1)
        mustHold(newUser(), s.id, s.seats[0])
        assertError(hold(newUser(), s.id, s.seats[0]), 409, "SEAT_NOT_AVAILABLE")
    }

    @Test
    @DisplayName("D0-04 unknown seat and schedule mismatch")
    fun D0_04_seatNotFound() {
        val s = seedSchedule(1)
        val other = seedSchedule(1)
        assertError(hold(newUser(), s.id, 999_999_999L), 404, "SEAT_NOT_FOUND", "unknown seat:")
        assertError(hold(newUser(), s.id, other.seats[0]), 404, "SEAT_NOT_FOUND", "seat of another schedule:")
    }

    @Test
    @DisplayName("D0-05 two seats per user (holds + confirmed)")
    fun D0_05_perUserLimit() {
        val s = seedSchedule(6)
        val a = newUser()
        mustHold(a, s.id, s.seats[0])
        mustHold(a, s.id, s.seats[1])
        assertError(hold(a, s.id, s.seats[2]), 409, "HOLD_LIMIT_EXCEEDED", "third hold:")

        val b = newUser()
        val h = mustHold(b, s.id, s.seats[3])
        val c = confirm(b, h)
        check(c.status == 200) { "precondition: confirm expected 200 but was $c" }
        mustHold(b, s.id, s.seats[4])
        assertError(hold(b, s.id, s.seats[5]), 409, "HOLD_LIMIT_EXCEEDED", "1 confirmed + 1 hold, third:")
    }

    @Test
    @DisplayName("D0-06 seat check before limit check")
    fun D0_06_seatBeforeLimit() {
        val s = seedSchedule(3)
        val a = newUser()
        mustHold(a, s.id, s.seats[0])
        mustHold(a, s.id, s.seats[1])
        mustHold(newUser(), s.id, s.seats[2])
        assertError(hold(a, s.id, s.seats[2]), 409, "SEAT_NOT_AVAILABLE")
    }

    @Test
    @DisplayName("D0-07 confirm success")
    fun D0_07_confirmSuccess() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        val r = confirm(u, h, "pay-d0-07")
        assertEquals(200, r.status, "status: $r")
        val json = assertNotNull(r.json, "body: $r")
        assertTrue(json.get("reservationId")?.isNumber == true, "reservationId must be a number: $r")
        assertTrue(json.get("seatId")?.isNumber == true && json.get("seatId").asLong() == s.seats[0], "seatId: $r")
        assertEquals("CONFIRMED", json.get("status")?.asText(), "status field: $r")
        assertEquals("RESERVED", seatStatus(s.seats[0]), "seat status")
        assertEquals(false, holdExists(h), "hold row must be deleted")
        val rows = jdbc.queryForList("SELECT payment_uid, status FROM reservation WHERE seat_id = ?", s.seats[0])
        assertEquals(1, rows.size, "reservation rows: $rows")
        assertEquals("pay-d0-07", rows[0]["payment_uid"])
        assertEquals("CONFIRMED", rows[0]["status"])
    }

    @Test
    @DisplayName("D0-08 confirm failure codes")
    fun D0_08_confirmFailures() {
        val s = seedSchedule(1)
        val owner = newUser()
        val h = mustHold(owner, s.id, s.seats[0])
        assertError(confirm(owner, 999_999_999L), 404, "HOLD_NOT_FOUND", "unknown hold:")
        assertError(confirm(newUser(), h), 403, "HOLD_NOT_OWNED", "other user's hold:")
    }

    // ------------------------------------------------------------------ S2

    @Test
    @DisplayName("D2-01 same seat, 30 concurrent holds")
    fun D2_01_sameSeatConcurrent() {
        val s = seedSchedule(1)
        val users = (0 until 30).map { newUser() }
        val results = concurrently(30) { i -> hold(users[i], s.id, s.seats[0]) }
        val created = results.count { it.status == 201 }
        val rest = results.filter { it.status != 201 }
        assertEquals(1, created, "201 count: ${results.groupingBy { it.status }.eachCount()}")
        assertTrue(results.none { it.status >= 500 || it.status < 0 }, "5xx/timeout: ${results.filter { it.status >= 500 || it.status < 0 }}")
        assertTrue(rest.all { it.status == 409 && it.code == "SEAT_NOT_AVAILABLE" }, "losers: ${rest.filter { !(it.status == 409 && it.code == "SEAT_NOT_AVAILABLE") }}")
        assertEquals(1, holdRowsForSeat(s.seats[0]), "hold rows for the seat")
    }

    @Test
    @DisplayName("D2-02 hold does not wait on a locked seat row")
    fun D2_02_noWait() {
        val s = seedSchedule(1)
        val u = newUser()
        val pool = Executors.newSingleThreadExecutor()
        val conn = rawConnection()
        try {
            conn.autoCommit = false
            conn.prepareStatement("SELECT id FROM product_seat WHERE id = ? FOR UPDATE").use { ps ->
                ps.setLong(1, s.seats[0])
                ps.executeQuery().use { check(it.next()) { "precondition: seat row" } }
            }
            val future = pool.submit<Resp> { hold(u, s.id, s.seats[0]) }
            val r = try {
                future.get(2, TimeUnit.SECONDS)
            } catch (e: TimeoutException) {
                conn.rollback()
                fail("hold did not answer within 2s while the seat row was locked (waiting)")
            }
            assertError(r, 409, "SEAT_NOT_AVAILABLE")
        } finally {
            runCatching { conn.rollback() }
            conn.close()
            pool.shutdown()
            pool.awaitTermination(10, TimeUnit.SECONDS)
        }
    }

    // ------------------------------------------------------------------ S4

    @Test
    @DisplayName("D4-02 same user, 10 seats concurrently, exactly 2 (x5)")
    fun D4_02_sameUserConcurrent() {
        val problems = mutableListOf<String>()
        repeat(5) { round ->
            val s = seedSchedule(10)
            val u = newUser()
            val results = concurrently(10) { i -> hold(u, s.id, s.seats[i]) }
            val created = results.count { it.status == 201 }
            val bad = results.filter { it.status != 201 && !(it.status == 409 && it.code == "HOLD_LIMIT_EXCEEDED") }
            val cnt = quotaCnt(s.id, u)
            if (created != 2 || bad.isNotEmpty() || cnt != 2) {
                problems += "round $round: created=$created cnt=$cnt unexpected=$bad"
            }
        }
        assertTrue(problems.isEmpty(), problems.joinToString("\n"))
    }

    @Test
    @DisplayName("D4-03 counter = holds + confirmed")
    fun D4_03_counterAfterConfirm() {
        val s = seedSchedule(2)
        val u = newUser()
        val h1 = mustHold(u, s.id, s.seats[0])
        mustHold(u, s.id, s.seats[1])
        val c = confirm(u, h1)
        check(c.status == 200) { "precondition: confirm expected 200 but was $c" }
        assertEquals(2, quotaCnt(s.id, u), "cnt")
        val actual = jdbc.queryForObject(
            "SELECT (SELECT count(*) FROM seat_hold WHERE schedule_id = ? AND user_id = ?) + " +
                "(SELECT count(*) FROM reservation WHERE schedule_id = ? AND user_id = ? AND status = 'CONFIRMED')",
            Int::class.java, s.id, u, s.id, u,
        )
        assertEquals(2, actual, "holds + confirmed")
    }

    @Test
    @DisplayName("D4-04 losing on the seat leaves the counter unchanged")
    fun D4_04_seatLossNoIncrement() {
        val s = seedSchedule(2)
        val x = s.seats[1]
        val problems = mutableListOf<String>()

        // Case 1: user with one hold already (cnt = 1).
        val b = newUser()
        mustHold(b, s.id, s.seats[0])
        val before = quotaCnt(s.id, b)
        val r1 = holdWhileSeatRowLocked(b, s.id, x)
        if (!(r1.status == 409 && r1.code == "SEAT_NOT_AVAILABLE")) problems += "existing user: expected 409 SEAT_NOT_AVAILABLE but was $r1"
        val after = quotaCnt(s.id, b)
        if (before != after) problems += "existing user: cnt $before -> $after"

        // Case 2: user never seen (no quota row before).
        val c = newUser()
        val r2 = holdWhileSeatRowLocked(c, s.id, x)
        if (!(r2.status == 409 && r2.code == "SEAT_NOT_AVAILABLE")) problems += "new user: expected 409 SEAT_NOT_AVAILABLE but was $r2"
        val afterNew = quotaCnt(s.id, c)
        if (afterNew != null && afterNew != 0) problems += "new user: cnt null -> $afterNew"

        assertEquals(0, holdRowsForSeat(x), "no hold row may be created on the locked seat")
        assertTrue(problems.isEmpty(), problems.joinToString("\n"))
    }

    /**
     * Holds the seat row with FOR UPDATE in a separate transaction, sends the hold from another thread
     * and waits at most 2s. The lock is released before returning; a waiting request yields status -1.
     */
    private fun holdWhileSeatRowLocked(userId: Long, scheduleId: Long, seatId: Long): Resp {
        val pool = Executors.newSingleThreadExecutor()
        val conn = rawConnection()
        try {
            conn.autoCommit = false
            conn.prepareStatement("SELECT id FROM product_seat WHERE id = ? FOR UPDATE").use { ps ->
                ps.setLong(1, seatId)
                ps.executeQuery().use { check(it.next()) { "precondition: seat row" } }
            }
            val future = pool.submit<Resp> { hold(userId, scheduleId, seatId) }
            val r = try {
                future.get(2, TimeUnit.SECONDS)
            } catch (e: TimeoutException) {
                Resp(-1, "TIMEOUT: hold waited more than 2s on the locked seat row")
            }
            conn.rollback()
            if (r.status == -1) runCatching { future.get(10, TimeUnit.SECONDS) }
            return r
        } finally {
            runCatching { conn.rollback() }
            conn.close()
            pool.shutdown()
            pool.awaitTermination(10, TimeUnit.SECONDS)
        }
    }

    @Test
    @DisplayName("D4-05 user at limit does not take the seat lock")
    fun D4_05_fullUserDoesNotLockSeat() {
        val s = seedSchedule(3)
        val u = newUser()
        val r = newUser()
        mustHold(u, s.id, s.seats[0])
        mustHold(u, s.id, s.seats[1])
        val x = s.seats[2]
        val pool = Executors.newSingleThreadExecutor()
        val conn = rawConnection()
        try {
            conn.autoCommit = false
            val lockedPid: Int
            try {
                conn.prepareStatement("SELECT cnt FROM user_hold_quota WHERE schedule_id = ? AND user_id = ? FOR UPDATE").use { ps ->
                    ps.setLong(1, s.id)
                    ps.setLong(2, u)
                    ps.executeQuery().use { rs ->
                        if (!rs.next()) throw TestAbortedException("UNSCORABLE no quota row for the user at limit")
                    }
                }
                lockedPid = conn.createStatement().use { st ->
                    st.executeQuery("SELECT pg_backend_pid()").use { rs -> rs.next(); rs.getInt(1) }
                }
            } catch (e: SQLException) {
                throw TestAbortedException("UNSCORABLE cannot lock quota row: ${e.message}", e)
            }
            val uFuture = pool.submit<Resp> { hold(u, s.id, x) }
            val waiting = eventually(Duration.ofSeconds(2)) {
                jdbc.queryForObject(
                    "SELECT count(*) FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid))", Int::class.java, lockedPid,
                )!! > 0
            }
            if (!waiting) {
                conn.rollback()
                val uResult = runCatching { uFuture.get(10, TimeUnit.SECONDS) }.getOrNull()
                throw TestAbortedException("UNSCORABLE request of the user at limit was not seen waiting on the quota row (result: $uResult)")
            }
            val rResult = concurrently(1, 10) { hold(r, s.id, x) }.single()
            conn.rollback()
            val uResult = try {
                uFuture.get(10, TimeUnit.SECONDS)
            } catch (e: TimeoutException) {
                Resp(-1, "TIMEOUT")
            }
            assertEquals(201, rResult.status, "other user's hold while the full user waits: $rResult")
            assertError(uResult, 409, "SEAT_NOT_AVAILABLE", "full user after release:")
        } finally {
            runCatching { conn.rollback() }
            conn.close()
            pool.shutdown()
            pool.awaitTermination(10, TimeUnit.SECONDS)
        }
    }

    // ------------------------------------------------------------------ S5

    @Test
    @DisplayName("D5-01 closed schedule rejects hold")
    fun D5_01_closedScheduleRejects() {
        val s = seedSchedule(1, Instant.now().minus(Duration.ofMinutes(1)))
        val u = newUser()
        assertError(hold(u, s.id, s.seats[0]), 409, "SCHEDULE_CLOSED")
        assertEquals(null, quotaCnt(s.id, u), "quota row must not be created")
    }

    @Test
    @DisplayName("D5-02 order: closed before seat before limit")
    fun D5_02_closedFirst() {
        val s = seedSchedule(4, Instant.now().minus(Duration.ofMinutes(1)))
        val other = newUser()
        seedHold(s.id, s.seats[0], other)
        val full = newUser()
        seedHold(s.id, s.seats[1], full)
        seedHold(s.id, s.seats[2], full)
        seedQuota(s.id, full, 2)
        assertError(hold(newUser(), s.id, s.seats[0]), 409, "SCHEDULE_CLOSED", "HELD seat in closed schedule:")
        assertError(hold(full, s.id, s.seats[3]), 409, "SCHEDULE_CLOSED", "user at limit in closed schedule:")
    }

    @Test
    @DisplayName("D5-03 confirm is allowed after close")
    fun D5_03_confirmAfterClose() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        seed { jdbc.update("UPDATE product_schedule SET starts_at = ? WHERE id = ?", ts(Instant.now().minus(Duration.ofMinutes(1))), s.id) }
        val r = confirm(u, h)
        assertEquals(200, r.status, "confirm after close: $r")
    }

    // ------------------------------------------------------------------ S6

    @Test
    @DisplayName("D6-02 same hold confirmed twice concurrently (x10)")
    fun D6_02_concurrentConfirm() {
        val problems = mutableListOf<String>()
        val s = seedSchedule(10)
        repeat(10) { round ->
            val u = newUser()
            val seat = s.seats[round]
            val h = mustHold(u, s.id, seat)
            val results = concurrently(2) { confirm(u, h) }
            val statuses = results.map { it.status }.sorted()
            val notFound = results.singleOrNull { it.status == 404 }
            val reservations = jdbc.queryForObject("SELECT count(*) FROM reservation WHERE seat_id = ?", Int::class.java, seat)
            if (statuses != listOf(200, 404) || notFound?.code != "HOLD_NOT_FOUND" || reservations != 1) {
                problems += "round $round: $results reservations=$reservations"
            }
        }
        assertTrue(problems.isEmpty(), problems.joinToString("\n"))
    }

    @Test
    @DisplayName("D6-03 partial unique index blocks duplicate CONFIRMED")
    fun D6_03_partialUniqueIndex() {
        val s = seedSchedule(1)
        val seat = s.seats[0]
        val insert = "INSERT INTO reservation (schedule_id, seat_id, user_id, payment_uid, status, created_at) VALUES (?, ?, ?, ?, ?, ?)"
        seed { jdbc.update(insert, s.id, seat, newUser(), "p-1", "CONFIRMED", ts(Instant.now())) }
        rawConnection().use { conn ->
            val state = try {
                conn.prepareStatement(insert).use { ps ->
                    ps.setLong(1, s.id); ps.setLong(2, seat); ps.setLong(3, newUser()); ps.setString(4, "p-2")
                    ps.setString(5, "CONFIRMED"); ps.setObject(6, ts(Instant.now()))
                    ps.executeUpdate()
                }
                null
            } catch (e: SQLException) {
                e.sqlState
            }
            assertEquals("23505", state, "second CONFIRMED for the same seat must fail with 23505")
        }
        jdbc.update(insert, s.id, seat, newUser(), "p-3", "CANCELED", ts(Instant.now()))
        jdbc.update(insert, s.id, seat, newUser(), "p-4", "CANCELED", ts(Instant.now()))
    }

    @Test
    @DisplayName("D6-10 unique violation on confirm maps to 409 SEAT_NOT_HELD")
    fun D6_10_uniqueViolationMapped() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        seed {
            jdbc.update(
                "INSERT INTO reservation (schedule_id, seat_id, user_id, payment_uid, status, created_at) VALUES (?, ?, ?, ?, 'CONFIRMED', ?)",
                s.id, s.seats[0], newUser(), "p-other", ts(Instant.now()),
            )
        }
        val r = confirm(u, h)
        assertTrue(r.status < 500, "must not be 5xx: $r")
        assertError(r, 409, "SEAT_NOT_HELD")
    }

    // ------------------------------------------------------------------ S7

    @Test
    @DisplayName("D7-03 payment-start first call extends")
    fun D7_03_paymentStartExtends() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        val before = Instant.now()
        val r = paymentStart(u, h)
        val after = Instant.now()
        assertEquals(200, r.status, "status: $r")
        val json = assertNotNull(r.json, "body: $r")
        assertEquals(true, json.get("extended")?.takeIf { it.isBoolean }?.asBoolean(), "extended: $r")
        val expiresAt = assertNotNull(parseTime(json.get("expiresAt")), "expiresAt: $r")
        val payBy = assertNotNull(parseTime(json.get("payBy")), "payBy: $r")
        assertTrue(offsetWithin(expiresAt, before, after, 609, 612), "expiresAt - request time not in [609s, 612s]: $r")
        assertTrue(Duration.between(payBy, expiresAt).minusSeconds(1).abs().toMillis() <= 1, "payBy must be expiresAt - 1s: $r")
        val within = json.get("payWithinSeconds")
        assertTrue(within != null && within.isIntegralNumber && within.asLong() in 608..610, "payWithinSeconds in [608, 610]: $r")
        val extendedAt = jdbc.queryForObject("SELECT extended_at IS NOT NULL FROM seat_hold WHERE id = ?", Boolean::class.java, h)
        assertEquals(true, extendedAt, "extended_at must be set")
    }

    @Test
    @DisplayName("D7-04 payment-start is idempotent")
    fun D7_04_paymentStartIdempotent() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        val first = paymentStart(u, h)
        check(first.status == 200) { "precondition: first payment-start 200 but was $first" }
        val firstExpires = checkNotNull(parseTime(first.json?.get("expiresAt"))) { "precondition: first expiresAt: $first" }
        val second = paymentStart(u, h)
        assertEquals(200, second.status, "second call: $second")
        val secondExpires = assertNotNull(parseTime(second.json?.get("expiresAt")), "expiresAt: $second")
        assertTrue(Duration.between(firstExpires, secondExpires).abs().toMillis() <= 1, "expiresAt changed: $first -> $second")
        assertEquals(true, second.json?.get("extended")?.takeIf { it.isBoolean }?.asBoolean(), "extended: $second")
    }

    @Test
    @DisplayName("D7-06 hold 201 carries payBy")
    fun D7_06_holdCarriesPayBy() {
        val s = seedSchedule(1)
        val r = hold(newUser(), s.id, s.seats[0])
        assertEquals(201, r.status, "status: $r")
        val json = assertNotNull(r.json, "body: $r")
        val expiresAt = assertNotNull(parseTime(json.get("expiresAt")), "expiresAt: $r")
        val payBy = assertNotNull(parseTime(json.get("payBy")), "payBy: $r")
        assertTrue(Duration.between(payBy, expiresAt).minusSeconds(1).abs().toMillis() <= 1, "payBy must be expiresAt - 1s: $r")
        val within = json.get("payWithinSeconds")
        assertTrue(within != null && within.isIntegralNumber && within.asLong() in 297..299, "payWithinSeconds in [297, 299]: $r")
    }
}
