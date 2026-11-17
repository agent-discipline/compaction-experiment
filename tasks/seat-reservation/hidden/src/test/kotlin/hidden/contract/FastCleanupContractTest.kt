package hidden.contract

import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import org.springframework.boot.test.context.SpringBootTest
import java.time.Duration
import java.time.Instant
import java.util.concurrent.TimeUnit
import kotlin.test.assertTrue

/** Fast quota cleanup for closed schedules (TTL and expiry interval stay at the application's values). */
@SpringBootTest(
    classes = [com.example.booking.BookingApplication::class],
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["seat.hold.counter-drift.cleanup.interval=1s"],
)
@Timeout(value = 120, unit = TimeUnit.SECONDS)
class FastCleanupContractTest : ContractTestBase() {

    @Test
    @DisplayName("D5-08 cleanup removes quota rows of a closed schedule without holds")
    fun D5_08_cleanupClosedWithoutHolds() {
        val s = seedSchedule(1, Instant.now().minus(Duration.ofMinutes(1)))
        seedQuota(s.id, newUser(), 1)
        seedQuota(s.id, newUser(), 0)
        val done = eventually(Duration.ofSeconds(3)) { quotaRowsForSchedule(s.id) == 0 }
        assertTrue(done, "after 3s quota rows of the closed schedule: ${quotaRowsForSchedule(s.id)}")
    }

    @Test
    @DisplayName("D5-09 cleanup keeps quota rows while holds remain")
    fun D5_09_cleanupKeepsWhileHolds() {
        val s = seedSchedule(1)
        val u = newUser()
        mustHold(u, s.id, s.seats[0])
        check(quotaCnt(s.id, u) != null) { "precondition: quota row created by the hold" }
        seed { jdbc.update("UPDATE product_schedule SET starts_at = ? WHERE id = ?", ts(Instant.now().minus(Duration.ofMinutes(1))), s.id) }
        Thread.sleep(3000)
        assertTrue(quotaCnt(s.id, u) != null, "quota row was removed while a hold remains")
    }
}
