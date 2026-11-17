package hidden.contract

import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import org.springframework.boot.test.context.SpringBootTest
import java.time.Duration
import java.util.concurrent.TimeUnit
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/** Extended hold outlives its original expiry and the batch. */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["seat.hold.ttl=3s", "seat.hold.expiry-interval=1s", "seat.confirm.payment-window=30s"],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class ExtendedHoldContractTest : ContractTestBase() {

    @Test
    @DisplayName("D7-07 extended hold survives the batch and confirms")
    fun D7_07_extendedHoldSurvives() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        val ps = paymentStart(u, h)
        check(ps.status == 200) { "precondition: payment-start 200 but was $ps" }
        Thread.sleep(6000)
        assertTrue(holdExists(h), "hold row must still exist after 6s")
        assertEquals("HELD", seatStatus(s.seats[0]), "seat status")
        val c = confirm(u, h)
        assertEquals(200, c.status, "confirm: $c")
    }
}

/** Payment window shorter than the remaining TTL. */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["seat.confirm.payment-window=60s"],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class ShortPaymentWindowContractTest : ContractTestBase() {

    @Test
    @DisplayName("D7-09 hold with enough time left is not extended")
    fun D7_09_enoughTimeLeft() {
        val s = seedSchedule(1)
        val u = newUser()
        val held = hold(u, s.id, s.seats[0])
        check(held.status == 201) { "precondition: hold 201 but was $held" }
        val h = held.json!!.get("holdId").asLong()
        val original = checkNotNull(parseTime(held.json!!.get("expiresAt"))) { "precondition: expiresAt: $held" }
        val r = paymentStart(u, h)
        assertEquals(200, r.status, "status: $r")
        assertEquals(false, r.json?.get("extended")?.takeIf { it.isBoolean }?.asBoolean(), "extended: $r")
        val expiresAt = assertNotNull(parseTime(r.json?.get("expiresAt")), "expiresAt: $r")
        assertTrue(Duration.between(original, expiresAt).abs().toMillis() <= 1, "expiresAt changed: $held -> $r")
        val extendedAtNull = jdbc.queryForObject("SELECT extended_at IS NULL FROM seat_hold WHERE id = ?", Boolean::class.java, h)
        assertEquals(true, extendedAtNull, "extended_at must stay NULL")
    }
}

/** Too little time left to extend. */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = [
        "seat.hold.ttl=3s",
        "seat.hold.expiry-interval=1h",
        "seat.confirm.payment-min-remaining=2s",
        "seat.confirm.pay-by-margin=1s",
    ],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class LateExtendContractTest : ContractTestBase() {

    @Test
    @DisplayName("D7-10 too late to extend")
    fun D7_10_tooLateToExtend() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        Thread.sleep(1500)
        assertError(paymentStart(u, h), 409, "HOLD_EXPIRED")
        val extendedAtNull = jdbc.queryForObject("SELECT extended_at IS NULL FROM seat_hold WHERE id = ?", Boolean::class.java, h)
        assertEquals(true, extendedAtNull, "extended_at must stay NULL")
    }
}
