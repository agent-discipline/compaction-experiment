package com.example.booking

import org.springframework.boot.SpringApplication
import org.springframework.boot.env.EnvironmentPostProcessor
import org.springframework.core.env.ConfigurableEnvironment
import org.springframework.core.env.MapPropertySource
import java.sql.DriverManager
import java.sql.SQLException
import java.util.UUID

/**
 * Tests only: every application context started by the tests gets its own new, empty database
 * on the PostgreSQL server that DB_URL points to, so test runs never see each other's schema or rows.
 * Old test databases that nobody is connected to any more are dropped before the first one is created.
 */
class TestDatabaseIsolation : EnvironmentPostProcessor {

    override fun postProcessEnvironment(environment: ConfigurableEnvironment, application: SpringApplication) {
        val url = environment.getProperty("DB_URL") ?: return
        val match = URL_PATTERN.matchEntire(url) ?: return
        val user = environment.getProperty("DB_USERNAME")
        val password = environment.getProperty("DB_PASSWORD")
        val name = PREFIX + UUID.randomUUID().toString().replace("-", "")
        DriverManager.getConnection(url, user, password).use { connection ->
            connection.createStatement().use { statement ->
                if (!sweptOldDatabases) {
                    sweptOldDatabases = true
                    val old = mutableListOf<String>()
                    statement.executeQuery("SELECT datname FROM pg_database WHERE datname LIKE '${PREFIX}%'").use { rows ->
                        while (rows.next()) old += rows.getString(1)
                    }
                    for (db in old) {
                        try {
                            statement.execute("DROP DATABASE \"$db\"")
                        } catch (e: SQLException) {
                            // still in use by another test run: leave it
                        }
                    }
                }
                statement.execute("CREATE DATABASE \"$name\"")
            }
        }
        val isolated = match.groupValues[1] + name + match.groupValues[3]
        environment.propertySources.addFirst(MapPropertySource("testDatabaseIsolation", mapOf("DB_URL" to isolated)))
    }

    companion object {
        private const val PREFIX = "test_"
        private val URL_PATTERN = Regex("^(jdbc:postgresql://[^/]+/)([^?]*)(.*)$")

        @Volatile
        private var sweptOldDatabases = false
    }
}
