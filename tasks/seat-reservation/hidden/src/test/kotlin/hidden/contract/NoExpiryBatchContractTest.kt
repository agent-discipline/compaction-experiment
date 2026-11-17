package hidden.contract

import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import org.springframework.boot.test.context.SpringBootTest
import java.util.concurrent.TimeUnit
import kotlin.test.assertTrue

/** Short TTL, expiry batch effectively off: holds expire but stay in the table. */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["seat.hold.ttl=2s", "seat.hold.expiry-interval=1h"],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class NoExpiryBatchContractTest : ContractTestBase() {

    @Test
    @DisplayName("D0-11 failure body has code and message")
    fun D0_11_failureBodyShape() {
        val s = seedSchedule(7)
        val responses = linkedMapOf<String, Resp>()

        // SEAT_NOT_HELD first: needs a live hold (TTL 2s).
        val d = newUser()
        val hd = mustHold(d, s.id, s.seats[4])
        seed { jdbc.update("UPDATE product_seat SET status = 'AVAILABLE' WHERE id = ?", s.seats[4]) }
        responses["SEAT_NOT_HELD"] = confirm(d, hd)

        val e = newUser()
        val he = mustHold(e, s.id, s.seats[5])

        responses["SEAT_NOT_FOUND"] = hold(newUser(), s.id, 999_999_999L)
        val a = newUser()
        val ha = mustHold(a, s.id, s.seats[0])
        responses["SEAT_NOT_AVAILABLE"] = hold(newUser(), s.id, s.seats[0])
        val c = newUser()
        mustHold(c, s.id, s.seats[1])
        mustHold(c, s.id, s.seats[2])
        responses["HOLD_LIMIT_EXCEEDED"] = hold(c, s.id, s.seats[3])
        responses["HOLD_NOT_FOUND"] = confirm(a, 999_999_999L)
        responses["HOLD_NOT_OWNED"] = confirm(newUser(), ha)

        Thread.sleep(3000)
        responses["HOLD_EXPIRED"] = confirm(e, he)

        val bad = responses.filter { (_, r) ->
            val json = r.json
            json == null || !json.isObject || !json.has("code") || !json.has("message") || !json.get("code").isTextual
        }
        assertTrue(bad.isEmpty(), "responses without {code(string), message}: $bad")
    }

    @Test
    @DisplayName("D0-13 expired hold (before batch) cannot be confirmed")
    fun D0_13_expiredHoldConfirm() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        Thread.sleep(3000)
        assertError(confirm(u, h), 409, "HOLD_EXPIRED")
    }

    @Test
    @DisplayName("D0-14 seat of an expired hold (before batch) is still unavailable")
    fun D0_14_expiredHoldSeatStillUnavailable() {
        val s = seedSchedule(1)
        mustHold(newUser(), s.id, s.seats[0])
        Thread.sleep(3000)
        assertError(hold(newUser(), s.id, s.seats[0]), 409, "SEAT_NOT_AVAILABLE")
    }

    @Test
    @DisplayName("D7-05 payment-start failure codes")
    fun D7_05_paymentStartFailures() {
        val s = seedSchedule(1)
        val owner = newUser()
        val h = mustHold(owner, s.id, s.seats[0])
        assertError(paymentStart(newUser(), h), 403, "HOLD_NOT_OWNED", "other user's hold:")
        assertError(paymentStart(owner, 999_999_999L), 404, "HOLD_NOT_FOUND", "unknown hold:")
        Thread.sleep(3000)
        assertError(paymentStart(owner, h), 409, "HOLD_EXPIRED", "expired hold:")
    }
}
