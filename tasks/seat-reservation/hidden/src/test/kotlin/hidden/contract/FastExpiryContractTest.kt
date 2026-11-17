package hidden.contract

import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import org.springframework.boot.test.context.SpringBootTest
import java.time.Duration
import java.util.concurrent.TimeUnit
import kotlin.test.assertTrue

/** Short TTL with a fast expiry batch. */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["seat.hold.ttl=2s", "seat.hold.expiry-interval=1s"],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class FastExpiryContractTest : ContractTestBase() {

    @Test
    @DisplayName("D0-09 expiry batch releases the seat")
    fun D0_09_expiryBatch() {
        val s = seedSchedule(1)
        val h = mustHold(newUser(), s.id, s.seats[0])
        val done = eventually(Duration.ofSeconds(4)) { seatStatus(s.seats[0]) == "AVAILABLE" && !holdExists(h) }
        assertTrue(done, "after 4s: seat=${seatStatus(s.seats[0])} holdRows=${holdRowsForSeat(s.seats[0])}")
    }

    @Test
    @DisplayName("D4-10 expiry decrements the counter")
    fun D4_10_expiryDecrements() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        val done = eventually(Duration.ofSeconds(4)) { !holdExists(h) && quotaCnt(s.id, u) == 0 }
        assertTrue(done, "after 4s: holdExists=${holdExists(h)} cnt=${quotaCnt(s.id, u)} (null = no row)")
    }

    @Test
    @DisplayName("D5-05 expiry survives a missing quota row")
    fun D5_05_missingQuotaRow() {
        val s = seedSchedule(2)
        val a = newUser()
        val b = newUser()
        mustHold(a, s.id, s.seats[0])
        mustHold(b, s.id, s.seats[1])
        seed { jdbc.update("DELETE FROM user_hold_quota WHERE schedule_id = ? AND user_id = ?", s.id, a) }
        val done = eventually(Duration.ofSeconds(4)) {
            seatStatus(s.seats[0]) == "AVAILABLE" && seatStatus(s.seats[1]) == "AVAILABLE" &&
                quotaCnt(s.id, a) == 0 && quotaCnt(s.id, b) == 0
        }
        assertTrue(
            done,
            "after 4s: seatA=${seatStatus(s.seats[0])} seatB=${seatStatus(s.seats[1])} cntA=${quotaCnt(s.id, a)} cntB=${quotaCnt(s.id, b)}",
        )
    }

    @Test
    @DisplayName("D5-06 expiry survives a counter lower than the holds")
    fun D5_06_counterTooLow() {
        val s = seedSchedule(2)
        val a = newUser()
        val b = newUser()
        mustHold(a, s.id, s.seats[0])
        mustHold(b, s.id, s.seats[1])
        var updated = 0
        seed { updated = jdbc.update("UPDATE user_hold_quota SET cnt = 0 WHERE schedule_id = ? AND user_id = ?", s.id, a) }
        check(updated == 1) { "precondition: quota row of A must exist (updated=$updated)" }
        val done = eventually(Duration.ofSeconds(4)) {
            seatStatus(s.seats[0]) == "AVAILABLE" && seatStatus(s.seats[1]) == "AVAILABLE" &&
                quotaCnt(s.id, a) == 0 && quotaCnt(s.id, b) == 0
        }
        assertTrue(
            done,
            "after 4s: seatA=${seatStatus(s.seats[0])} seatB=${seatStatus(s.seats[1])} cntA=${quotaCnt(s.id, a)} cntB=${quotaCnt(s.id, b)}",
        )
    }

    @Test
    @DisplayName("D6-05 confirm after the batch removed the hold")
    fun D6_05_confirmAfterBatch() {
        val s = seedSchedule(1)
        val u = newUser()
        val h = mustHold(u, s.id, s.seats[0])
        eventually(Duration.ofSeconds(4)) { !holdExists(h) }
        assertError(confirm(u, h), 404, "HOLD_NOT_FOUND")
    }
}
