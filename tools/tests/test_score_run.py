import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import assistant, git, user, write, write_jsonl

import run_task
import score_run as sr

T0 = 1_700_000_000
BUILD = 'plugins {\n  kotlin("jvm") version "2.2.20"\n}\ndependencies {\n  implementation("a:b")\n}\n'
YML_S3 = "seat:\n  hold:\n    limit-strategy: advisory-try\n"
YML_FINAL = """\
spring:
  datasource:
    hikari:
      maximum-pool-size: 10
seat:
  hold:
    ttl: PT5M
    max-per-user: 2
    expiry-interval: 10000
    strategy: pessimistic-nowait
    limit-strategy: COUNTER_UPSERT_PRECHECK
"""
REPO = """\
package com.example.booking.seat.repo

// SELECT pg_try_advisory_xact_lock(1, 2)  ← 주석은 무시
class QuotaRepo {
    val upsert = \"\"\"
        INSERT INTO user_hold_quota (schedule_id, user_id, cnt) VALUES (?, ?, 1)
        ON CONFLICT (schedule_id, user_id) DO UPDATE SET cnt = user_hold_quota.cnt + 1
        WHERE user_hold_quota.cnt + 1 <= ?
    \"\"\"
    val lock = "SELECT status FROM product_seat WHERE id = ? FOR UPDATE NOWAIT"
    val bad = "UPDATE seat_hold SET held_at = now()"
    fun t() = java.time.Instant.now()
}
"""
CONF = """\
package com.example.booking.config

class ClockConfig {
    @Bean
    fun clock(): Clock = Clock.systemUTC()
}
"""
TESTS = """\
package com.example.booking.seat

class HoldTest {
    @Test
    fun `같은 좌석을 두 명이 잡으면 한 명만 성공한다`() {}

    @Test
    @DisplayName("x")
    fun englishName() {}

    @ParameterizedTest
    @ValueSource(ints = [1, 2])
    fun `매수 상한`(n: Int) {}
}
"""


def make_case(premise_s3_ok=True):
    base = Path(tempfile.mkdtemp())
    skel, work, rdir = base / "skel", base / "work", base / "run"
    write(skel, "build.gradle.kts", BUILD)
    write(work, "build.gradle.kts", BUILD)
    write(work, "src/main/kotlin/com/example/booking/BookingApplication.kt", "package com.example.booking\n")
    write(work, "src/test/kotlin/com/example/booking/BookingApplicationTests.kt",
          "package com.example.booking\nclass A { @Test fun contextLoads() {} }\n")
    write(work, "src/main/resources/db/migration/.gitkeep", "")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "skeleton", date=T0)
    skeleton = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()

    def commit(msg, date):
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", msg, date=date)

    write(work, "src/main/resources/db/migration/V1__init.sql", "CREATE TABLE product (id BIGSERIAL);\n")
    commit("[S0] 기본 구현", T0 + 10)
    write(work, "src/main/resources/db/migration/V2__add_indexes.sql", "CREATE INDEX a ON b (c);\n")
    commit("[S1] 인덱스", T0 + 110)
    write(work, "src/main/kotlin/com/example/booking/seat/Lock.kt",
          "package com.example.booking.seat\nval q = \"SELECT pg_try_advisory_xact_lock(?, ?)\"\n" if premise_s3_ok
          else "package com.example.booking.seat\n")
    write(work, "src/main/resources/application.yml", YML_S3)
    commit("[S3] 어드바이저리", T0 + 310)
    (work / "src/main/kotlin/com/example/booking/seat/Lock.kt").unlink()
    write(work, "src/main/resources/application.yml", YML_FINAL)
    write(work, "src/main/kotlin/com/example/booking/seat/repo/QuotaRepo.kt", REPO)
    write(work, "src/main/kotlin/com/example/booking/config/ClockConfig.kt", CONF)
    write(work, "src/test/kotlin/com/example/booking/seat/HoldTest.kt", TESTS)
    write(work, "src/main/resources/db/migration/V1__init.sql", "CREATE TABLE product (id BIGSERIAL, x INT);\n")
    commit("S4 카운터", T0 + 410)

    msgs = ["M0", "M1", "M2", "M3", "M4"]
    rows = [user("M0"), assistant("구현을 마쳤고 테스트가 통과했습니다. 다음 단계로 가겠습니다.", mid="a0"),
            user("M1"), assistant("인덱스 다섯 개를 추가했고 유니크가 아님을 확인했습니다.", mid="a1"),
            {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto"}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "요약"}},
            user("M2"), assistant("I added the pessimistic lock and all the tests are passing now.", mid="a2"),
            user("M3"), assistant("어드바이저리 락을 넣었습니다. 테스트도 추가했습니다.", mid="a3"),
            user("M4"), assistant("완료", mid="a4")]
    (rdir / "turns" / "04").mkdir(parents=True)
    write_jsonl(rdir / "turns" / "04" / "transcript.jsonl", rows)
    ends = [2, 4, 8, 10, 12]
    entries = [json.loads(x) for x in (rdir / "turns" / "04" / "transcript.jsonl").read_text().splitlines()]
    import transcript
    bounds = transcript.locate_boundaries(entries, ends, msgs)
    run = {"run_id": "fake-run", "model": "m", "condition": "compact", "rep": 1, "status": "complete",
           "invalid": False, "invalid_reasons": [], "skeleton_commit": skeleton, "boundaries": bounds,
           "totals": {"tokens": 1}, "injections": [],
           "turns": [{"index": k, "step": f"S{k}", "started_at": T0 + 5 + 100 * k - (5 if k else 0),
                      "transcript_lines": ends[k]} for k in range(5)]}
    (rdir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    cfg = run_task.load_config()
    cfg["skeleton_dir"] = str(skel)
    cfg["hidden"] = dict(cfg["hidden"], items_file=str(base / "no-items.json"))
    return cfg, rdir, work


class ScoreRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg, rdir, work = make_case()
        cls.s = sr.score(cfg, rdir, work, skip_hidden=True, skip_db=True)
        cls.items = cls.s["items"]

    def r(self, item):
        return self.items[item]["result"]

    def test_scoring_table_parsed(self):
        t = sr.parse_scoring_items((Path(run_task.load_config()["task_dir"]) / "scoring-items.md")
                                   .read_text(encoding="utf-8"))
        self.assertEqual(t["D3-01"]["delivered_step"], 4)
        self.assertEqual(t["D7-01"]["delivered_step"], 7)
        self.assertEqual(t["D1-04"]["delivered_step"], 1)
        self.assertEqual(t["K7a"]["delivered_step"], 0)
        self.assertEqual(t["D5-11"]["kind"], "M")
        self.assertNotIn("D4-09", t)
        self.assertIn("D0-12", t)

    def test_config_items(self):
        self.assertEqual(self.r("D0-10"), "pass")   # PT5M · 2 · 10000(ms)
        self.assertEqual(self.r("D1-03"), "pass")
        self.assertEqual(self.r("D2-03"), "pass")
        self.assertEqual(self.r("D5-10"), "fail")   # 키 없음
        self.assertEqual(self.r("D7-01"), "fail")

    def test_regex_items(self):
        self.assertEqual(self.r("D4-06"), "pass")
        self.assertEqual(self.r("D2-04"), "pass")
        self.assertEqual(self.r("D3-01"), "pass")   # 주석 속 pg_try_advisory는 무시
        self.assertEqual(self.r("D6-06"), "fail")
        self.assertEqual(self.r("D4-08"), "fail")

    def test_premise(self):
        self.assertEqual(self.r("D3-00"), "pass")
        self.assertEqual(self.r("D3-02"), "pass")
        self.assertEqual(self.r("D6-00"), "unscorable")
        self.assertEqual(self.items["D7-01"].get("premise"), "unknown")

    def test_premise_unmet(self):
        cfg, rdir, work = make_case(premise_s3_ok=False)
        items = sr.score(cfg, rdir, work, skip_hidden=True, skip_db=True)["items"]
        self.assertEqual(items["D3-00"]["result"], "fail")
        self.assertEqual(items["D3-01"]["result"], "premise_unmet")
        self.assertEqual(items["D3-01"]["raw_result"], "pass")

    def test_k_items(self):
        k1 = self.items["K1"]
        self.assertEqual(k1["result"], "fail")
        self.assertEqual([v["file"] for v in k1["violations"]],
                         ["src/main/kotlin/com/example/booking/config/ClockConfig.kt"])
        k2 = self.items["K2"]
        self.assertEqual(k2["result"], "fail")
        self.assertEqual(k2["violations"][0]["status"], "M")
        self.assertEqual(self.r("K3"), "pass")
        self.assertEqual(self.r("K4"), "pass")
        k5 = self.items["K5"]
        self.assertEqual((k5["result"], k5["total"], k5["violation_count"]), ("fail", 3, 1))
        k6 = self.items["K6"]
        self.assertEqual(k6["result"], "fail")
        self.assertEqual(sorted(v["match"] for v in k6["violations"]), ["Instant.now(", "now()"])
        self.assertEqual(len(k6["allowed_bean_clock"]), 1)

    def test_k7(self):
        a, b = self.items["K7a"], self.items["K7b"]
        self.assertEqual((a["result"], a["steps_without_commit"]), ("fail", [2]))
        self.assertEqual((b["result"], b["bad_prefix"], b["number_mismatch"]), ("fail", 1, 0))

    def test_k8_per_turn(self):
        k8 = self.items["K8"]
        self.assertEqual((k8["result"], k8["violating_turns"], k8["judged_turns"]), ("fail", [2], 4))
        turns = self.s["k8_turns"]
        self.assertIsNone(turns[4]["ratio"])          # 20자 미만
        self.assertEqual(turns[2]["compactions_so_far"], 1)

    def test_delivery_distance(self):
        self.assertEqual(self.s["compactions"], 1)
        self.assertEqual(self.s["boundaries"][0]["coord"], 1.5)
        d = self.items["D0-10"]
        self.assertEqual((d["delivered_step"], d["compactions_after_delivery"], d["turns_to_first_compaction"]),
                         (0, 1, 2))
        d = self.items["D2-03"]
        self.assertEqual((d["compactions_after_delivery"], d["turns_to_first_compaction"]), (0, None))

    def test_hidden_items_not_run_without_items_json(self):
        self.assertEqual(self.r("D0-02"), "not_run")
        self.assertFalse(self.s["hidden"]["ran"])

    def test_k4_detects_dependency_change(self):
        d = Path(tempfile.mkdtemp())
        write(d / "s", "build.gradle.kts", BUILD)
        write(d / "w", "build.gradle.kts", BUILD.replace('implementation("a:b")',
                                                         'implementation("a:b")\n  testImplementation("x:awaitility")'))
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "fail")
        write(d / "w", "build.gradle.kts", BUILD.replace("  implementation", "implementation") + "// 주석\n")
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "pass")


if __name__ == "__main__":
    unittest.main()
