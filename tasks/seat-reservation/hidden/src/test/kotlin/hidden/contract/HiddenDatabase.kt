package hidden.contract

import org.springframework.boot.SpringApplication
import org.springframework.boot.env.EnvironmentPostProcessor
import org.springframework.core.env.ConfigurableEnvironment
import org.springframework.core.env.MapPropertySource
import java.sql.DriverManager
import java.util.UUID

/**
 * Every SpringApplication started in the hidden-test JVM (one per test class through @DirtiesContext, plus each
 * programmatic start in StartupValidationContractTest) gets its own new, empty database on the scoring PostgreSQL
 * server. The server comes from the process environment (DB_URL / DB_USERNAME / DB_PASSWORD of the scoring
 * container), never from the application's configuration, and the connection settings are put above every other
 * property source (like a service connection): whatever the implementation configured, it runs against a clean
 * database of its own.
 */
class HiddenDatabase : EnvironmentPostProcessor {

    override fun postProcessEnvironment(environment: ConfigurableEnvironment, application: SpringApplication) {
        val admin = System.getenv("DB_URL") ?: error("hidden tests: DB_URL (scoring database server) is not set")
        val user = System.getenv("DB_USERNAME") ?: error("hidden tests: DB_USERNAME is not set")
        val password = System.getenv("DB_PASSWORD") ?: error("hidden tests: DB_PASSWORD is not set")
        val match = URL_PATTERN.matchEntire(admin) ?: error("hidden tests: DB_URL is not a jdbc:postgresql URL: $admin")
        val name = "hidden_" + UUID.randomUUID().toString().replace("-", "")
        DriverManager.getConnection(admin, user, password).use { c ->
            c.createStatement().use { it.execute("CREATE DATABASE \"$name\"") }
        }
        val url = match.groupValues[1] + name + match.groupValues[3]
        environment.propertySources.addFirst(
            MapPropertySource(
                "hiddenDatabase",
                mapOf(
                    "spring.datasource.url" to url,
                    "spring.datasource.username" to user,
                    "spring.datasource.password" to password,
                    "DB_URL" to url,
                    "DB_USERNAME" to user,
                    "DB_PASSWORD" to password,
                ),
            ),
        )
    }

    companion object {
        private val URL_PATTERN = Regex("^(jdbc:postgresql://[^/]+/)([^?]*)(.*)$")
    }
}
