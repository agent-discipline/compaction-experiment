import unittest
from decimal import Decimal

from helpers import TOOLS  # noqa: F401

import springconf as sc

YML = """\
spring:
  application:
    name: booking   # 주석
  datasource:
    url: ${DB_URL}
    hikari:
      maximumPoolSize: 10
seat:
  hold:
    ttl: 300s
    max_per_user: "2"
    expiry-interval: PT10S
    strategy: PESSIMISTIC_NOWAIT
  "confirm.mode": 'conditional-extend'
  counter-drift.cleanup:
    interval: ${CLEANUP:1m}
list:
  - a
  - b
---
spring:
  config:
    activate:
      on-profile: test
seat:
  hold:
    ttl: 2s
"""


class SpringConfTest(unittest.TestCase):
    def flat(self):
        return sc.load_from_texts({"application.yml": YML})[0]

    def test_nested_relaxed_lookup(self):
        f = self.flat()
        self.assertEqual(sc.lookup(f, "spring.datasource.hikari.maximum-pool-size"), (True, "10"))
        self.assertEqual(sc.lookup(f, "seat.hold.max-per-user"), (True, "2"))
        self.assertEqual(sc.lookup(f, "seat.confirm.mode"), (True, "conditional-extend"))
        self.assertEqual(sc.lookup(f, "seat.counter_drift.cleanup.interval"), (True, "1m"))
        self.assertEqual(sc.lookup(f, "spring.application.name"), (True, "booking"))
        self.assertEqual(sc.lookup(f, "list[1]"), (True, "b"))
        self.assertEqual(sc.lookup(f, "seat.nope")[0], False)

    def test_profile_document_ignored(self):
        self.assertEqual(sc.lookup(self.flat(), "seat.hold.ttl"), (True, "300s"))

    def test_properties_override_yaml(self):
        f, used = sc.load_from_texts({"application.yml": YML, "application.properties": "seat.hold.ttl=5m\n# c\n"})
        self.assertEqual(sc.lookup(f, "seat.hold.ttl"), (True, "5m"))
        self.assertEqual(used, ["application.yml", "application.properties"])

    def test_duration_equivalence(self):
        for v in ("5m", "300s", "PT5M", "300000", "300000ms", "pt5m"):
            self.assertEqual(sc.parse_duration(v), Decimal(300), v)
        self.assertEqual(sc.parse_duration("PT10M11S"), Decimal(611))
        self.assertEqual(sc.parse_duration("PT0.5S"), Decimal("0.5"))
        self.assertIsNone(sc.parse_duration("five minutes"))
        self.assertIsNone(sc.parse_duration("PT"))
        self.assertTrue(sc.matches("611s", 611, "duration"))
        self.assertFalse(sc.matches("610s", 611, "duration"))

    def test_enum_and_int(self):
        self.assertTrue(sc.matches("CONDITIONAL_EXTEND", "conditional-extend", "enum"))
        self.assertFalse(sc.matches("conditional", "conditional-extend", "enum"))
        self.assertTrue(sc.matches("10", 10, "int"))
        self.assertFalse(sc.matches("ten", 10, "int"))
        self.assertFalse(sc.matches(None, 10, "int"))

    def test_hash_in_quotes_not_comment(self):
        f = sc.load_from_texts({"application.yml": 'a:\n  b: "x # y"  # real\n'})[0]
        self.assertEqual(sc.lookup(f, "a.b"), (True, "x # y"))

    def test_unparseable_line_raises(self):
        with self.assertRaises(sc.ConfigError):
            sc.load_from_texts({"application.yml": "a:\n  just text without colon\n"})


if __name__ == "__main__":
    unittest.main()
