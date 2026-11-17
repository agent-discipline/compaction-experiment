package hidden.contract

import com.example.booking.BookingApplication
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.DisplayName
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.TestInstance
import org.junit.jupiter.api.Timeout
import org.springframework.boot.builder.SpringApplicationBuilder
import org.testcontainers.containers.PostgreSQLContainer
import org.testcontainers.utility.DockerImageName
import java.util.concurrent.TimeUnit
import kotlin.test.fail

/**
 * Starts the application programmatically against a real database and checks that an invalid
 * configuration fails at startup. A control start with the application's own configuration must succeed
 * first, so that a failure is attributable to the property under test.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 180, unit = TimeUnit.SECONDS)
class StartupValidationContractTest {

    private val postgres: PostgreSQLContainer<*> = PostgreSQLContainer(DockerImageName.parse("postgres:16"))

    @BeforeAll
    fun startDb() = postgres.start()

    @AfterAll
    fun stopDb() = postgres.stop()

    private val control: Throwable? by lazy { startOnce() }

    /** null = started (and closed) successfully. */
    private fun startOnce(vararg extra: String): Throwable? {
        val args = arrayOf(
            "--spring.datasource.url=${postgres.jdbcUrl}",
            "--spring.datasource.username=${postgres.username}",
            "--spring.datasource.password=${postgres.password}",
            "--server.port=0",
            *extra.map { "--$it" }.toTypedArray(),
        )
        return try {
            SpringApplicationBuilder(BookingApplication::class.java).run(*args).close()
            null
        } catch (t: Throwable) {
            t
        }
    }

    private fun assertStartupFails(vararg props: String) {
        control?.let { fail("control start with the application's configuration failed: $it", it) }
        if (startOnce(*props) == null) fail("application started with ${props.toList()}")
    }

    @Test
    @DisplayName("D2-05 unknown hold strategy fails startup")
    fun D2_05_unknownHoldStrategy() {
        assertStartupFails("seat.hold.strategy=pessimistic-wait")
    }

    @Test
    @DisplayName("D7-08 invalid payment settings fail startup")
    fun D7_08_invalidPaymentSettings() {
        control?.let { fail("control start with the application's configuration failed: $it", it) }
        val problems = mutableListOf<String>()
        if (startOnce("seat.confirm.pay-by-margin=5s", "seat.confirm.payment-min-remaining=1s") == null) {
            problems += "started with pay-by-margin=5s > payment-min-remaining=1s"
        }
        if (startOnce("seat.confirm.mode=conditional-extend", "seat.confirm.payment-window=0s") == null) {
            problems += "started with payment-window=0s (conditional-extend)"
        }
        if (problems.isNotEmpty()) fail(problems.joinToString("; "))
    }
}
