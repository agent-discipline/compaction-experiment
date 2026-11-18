import json
import os
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def head(work):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()


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
    skeleton = head(work)

    def commit(msg, date):
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", msg, date=date)
        return head(work)

    write(work, "src/main/resources/db/migration/V1__init.sql", "CREATE TABLE product (id BIGSERIAL);\n")
    h0 = commit("[S0] 기본 구현", T0 + 10)
    write(work, "src/main/resources/db/migration/V2__add_indexes.sql", "CREATE INDEX a ON b (c);\n")
    h1 = commit("[S1] 인덱스", T0 + 110)
    write(work, "src/main/kotlin/com/example/booking/seat/Lock.kt",
          "package com.example.booking.seat\nval q = \"SELECT pg_try_advisory_xact_lock(?, ?)\"\n" if premise_s3_ok
          else "package com.example.booking.seat\n")
    write(work, "src/main/resources/application.yml", YML_S3)
    h3 = commit("[S3] 어드바이저리", T0 + 310)
    (work / "src/main/kotlin/com/example/booking/seat/Lock.kt").unlink()
    write(work, "src/main/resources/application.yml", YML_FINAL)
    write(work, "src/main/kotlin/com/example/booking/seat/repo/QuotaRepo.kt", REPO)
    write(work, "src/main/kotlin/com/example/booking/config/ClockConfig.kt", CONF)
    write(work, "src/test/kotlin/com/example/booking/seat/HoldTest.kt", TESTS)
    write(work, "src/main/resources/db/migration/V1__init.sql", "CREATE TABLE product (id BIGSERIAL, x INT);\n")
    h4 = commit("S4 카운터", T0 + 410)
    heads = [h0, h1, h1, h3, h4]  # 턴 2(S2)는 커밋 없음

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
                      "transcript_lines": ends[k], "git_head": heads[k]} for k in range(5)]}
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
        self.assertEqual(sorted(v["match"] for v in k6["violations"]), ["Instant.now()", "now()"])
        self.assertEqual(len(k6["allowed_bean_clock"]), 1)

    def test_k7(self):
        a, b = self.items["K7a"], self.items["K7b"]
        self.assertEqual((a["result"], a["steps_without_commit"]), ("fail", [2]))
        self.assertEqual((b["result"], b["bad_prefix"], b["number_mismatch"]), ("fail", 1, 0))

    def test_k7b_number_mismatch_is_recorded_not_judged(self):
        w = Path(tempfile.mkdtemp())
        write(w, "a.txt", "0")
        git(w, "init", "-q", "-b", "main")
        git(w, "add", "-A")
        git(w, "commit", "-q", "-m", "skeleton", date=T0)
        skel = head(w)
        for k, msg in enumerate(["[S0] a", "[S5] 번호가 틀림"]):
            write(w, "a.txt", str(k + 1))
            git(w, "add", "-A")
            git(w, "commit", "-q", "-m", msg, date=T0 + 100 * k + 10)
        run = {"skeleton_commit": skel, "turns": [{"index": k, "step": f"S{k}", "started_at": T0 + 100 * k}
                                                  for k in range(2)]}
        _, b = sr.k7(w, run)
        self.assertEqual((b["result"], b["bad_prefix"], b["number_mismatch"]), ("pass", 0, 1))

    def test_k8_per_turn(self):
        k8 = self.items["K8"]
        self.assertEqual((k8["result"], k8["violating_turns"], k8["judged_turns"]), ("fail", [2], 4))
        turns = self.s["k8_turns"]
        self.assertIsNone(turns[4]["ratio"])          # 20자 미만
        self.assertEqual(turns[2]["compactions_so_far"], 1)

    def test_k8_segments_split_at_mid_turn_boundary(self):
        import transcript
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ko = "선점 경로의 조회를 인덱스로 바꾸고 테스트를 모두 다시 돌려서 통과하는 것을 확인했습니다. 다음 단계로 넘어가겠습니다."
        rows = [user("M0"), assistant(ko, mid="a0"),
                {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto"}},
                {"type": "attachment", "attachment": {"type": "date"}},
                {"type": "user", "isCompactSummary": True, "message": {"content": "요약"}},
                assistant("Now adding the migration file for the pool.", mid="a1"),      # 경계 뒤 영어(20자 넘음)
                user("M1"), assistant(ko, mid="a2"),
                {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto"}},
                {"type": "user", "isCompactSummary": True, "message": {"content": "요약"}},
                assistant("Done, ok.", mid="a3")]                                       # 경계 뒤 영어지만 20자 미만
        (d / "turns" / "01").mkdir(parents=True)
        write_jsonl(d / "turns" / "01" / "transcript.jsonl", rows)
        entries = transcript.load(d / "turns" / "01" / "transcript.jsonl")[0]
        run = {"boundaries": transcript.locate_boundaries(entries, [6, 11], ["M0", "M1"]),
               "turns": [{"index": 0, "step": "S0", "transcript_lines": 6}, {"index": 1, "step": "S1", "transcript_lines": 11}]}
        r, per = sr.k8(d, run, 0.5, 20)
        t0, t1 = per
        self.assertGreaterEqual(t0["ratio"], 0.5)                                     # 턴 전체로는 통과
        self.assertEqual([(s["start_line"], s["end_line"], s["after_boundary"]) for s in t0["segments"]],
                         [(0, 2, False), (2, 6, True)])
        self.assertEqual(t0["segments"][0]["ratio"], 1.0)
        self.assertLess(t0["segments"][1]["ratio"], 0.5)
        self.assertEqual((t0["turn_violation"], t0["segment_violation"], t0["violation"]), (False, True, True))
        self.assertIsNone(t1["segments"][1]["ratio"])                                  # 20자 미만 구간은 판정 제외
        self.assertEqual((t1["turn_violation"], t1["segment_violation"], t1["violation"]), (False, False, False))
        self.assertEqual((r["result"], r["violating_turns"], r["judged_turns"]), ("fail", [0], 2))
        self.assertEqual(r["turn_level"], {"result": "pass", "violating_turns": [], "judged_turns": 2, "rate": 0.0})

    def test_delivery_distance(self):
        self.assertEqual(self.s["compactions"], 1)
        self.assertEqual(self.s["boundaries"][0]["coord"], 1.5)
        d = self.items["D0-10"]
        self.assertEqual((d["delivered_step"], d["compactions_after_delivery"], d["turns_to_first_compaction"]),
                         (0, 1, 2))
        d = self.items["D2-03"]
        self.assertEqual((d["compactions_after_delivery"], d["turns_to_first_compaction"]), (0, None))

    def test_score_stamped_with_run_identity(self):
        import hashlib
        cfg, rdir, work = make_case()
        s = sr.score(cfg, rdir, work, skip_hidden=True, skip_db=True)
        self.assertEqual((s["run_id"], s["model"], s["condition"]), ("fake-run", "m", "compact"))
        self.assertEqual(s["run_sha256"], hashlib.sha256((rdir / "run.json").read_bytes()).hexdigest())

    def test_hidden_items_not_run_without_items_json(self):
        self.assertEqual(self.r("D0-02"), "not_run")
        self.assertFalse(self.s["hidden"]["ran"])

    def test_k4_settings_blocks_and_new_version_catalog(self):
        d = Path(tempfile.mkdtemp())
        for side in ("s", "w"):
            write(d / side, "build.gradle.kts", BUILD)
            write(d / side, "settings.gradle.kts", 'rootProject.name = "booking"\n')
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "pass")
        write(d / "w", "settings.gradle.kts", 'rootProject.name = "booking"\n'
                                             "dependencyResolutionManagement {\n  repositories { mavenCentral() }\n}\n")
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "fail")
        write(d / "w", "settings.gradle.kts", 'pluginManagement {\n  repositories { gradlePluginPortal() }\n}\n'
                                             'rootProject.name = "booking"\n')
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "fail")
        write(d / "w", "settings.gradle.kts", 'rootProject.name = "booking"\n')
        write(d / "w", "gradle/libs.versions.toml", '[libraries]\nx = "a:b:1"\n')
        r = sr.k4(d / "w", d / "s")
        self.assertEqual((r["result"], r["diffs"][0]["file"]), ("fail", "gradle/libs.versions.toml"))

    def test_k5_unicode_identifier_without_backticks_counted(self):
        d = Path(tempfile.mkdtemp())
        write(d, "src/test/kotlin/com/example/booking/seat/T.kt",
              "package com.example.booking.seat\nclass T {\n  @Test\n  fun 한글이지만백틱없음() {}\n"
              "  @Test\n  fun `한글 백틱`() {}\n}\n")
        r = sr.k5(d, set(), "src/test/kotlin/hidden/")
        self.assertEqual((r["total"], r["violation_count"], r["violations"][0]["name"]), (2, 1, "한글이지만백틱없음"))

    def test_k4_detects_dependency_change(self):
        d = Path(tempfile.mkdtemp())
        write(d / "s", "build.gradle.kts", BUILD)
        write(d / "w", "build.gradle.kts", BUILD.replace('implementation("a:b")',
                                                         'implementation("a:b")\n  testImplementation("x:awaitility")'))
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "fail")
        write(d / "w", "build.gradle.kts", BUILD.replace("  implementation", "implementation") + "// 주석\n")
        self.assertEqual(sr.k4(d / "w", d / "s")["result"], "pass")



def k6_of(src, rel="src/main/kotlin/com/example/booking/seat/A.kt"):
    d = Path(tempfile.mkdtemp())
    write(d, rel, src)
    return sr.k6(d)


class K6Test(unittest.TestCase):
    def test_now_with_clock_argument_passes(self):
        r = k6_of("package a\nclass A(val clock: Clock) {\n  fun t() = Instant.now(clock)\n"
                  "  fun u() = LocalDateTime.now(clock)\n  fun v() = OffsetDateTime.now( clock )\n}\n")
        self.assertEqual((r["result"], r["violations"]), ("pass", []))

    def test_no_arg_now_and_millis_fail(self):
        r = k6_of("package a\nfun t() = LocalDateTime.now( )\nfun u() = System.currentTimeMillis()\n")
        self.assertEqual(sorted(v["match"] for v in r["violations"]), ["LocalDateTime.now( )", "System.currentTimeMillis("])

    def test_clock_creation_outside_bean_fails(self):
        r = k6_of("package a\nclass S { val c = Clock.system(ZoneOffset.UTC) }\n")
        self.assertEqual((r["result"], [v["match"] for v in r["violations"]]), ("fail", ["Clock.system("]))

    def test_one_bean_clock_allowed_second_fails(self):
        one = "@Configuration\nclass C {\n  @Bean\n  fun clock(): Clock = Clock.systemDefaultZone()\n}\n"
        self.assertEqual(k6_of("package a\n" + one)["result"], "pass")
        two = one + "class D {\n  @Bean\n  fun clock2(): Clock = Clock.systemUTC()\n}\n"
        r = k6_of("package a\n" + two)
        self.assertEqual((r["result"], len(r["allowed_bean_clock"]), len(r["violations"])), ("fail", 1, 1))

    def test_sql_time_functions_in_strings(self):
        r = k6_of('package a\nval q = "UPDATE x SET t = statement_timestamp()"\nval ok = "SELECT :now"\n')
        self.assertEqual([v["match"] for v in r["violations"]], ["statement_timestamp("])

    def test_java_file_checked(self):
        r = k6_of("package a;\nclass J { long t() { return System.currentTimeMillis(); } }\n",
                  "src/main/java/a/J.java")
        self.assertEqual(r["result"], "fail")


class K1JavaTest(unittest.TestCase):
    def test_java_files_checked(self):
        d = Path(tempfile.mkdtemp())
        write(d, "src/main/kotlin/com/example/booking/seat/A.kt", "package com.example.booking.seat\n")
        write(d, "src/test/java/com/example/other/B.java", "package com.example.other;\nclass B {}\n")
        r = sr.k1(d, set(), "src/test/kotlin/hidden/")
        self.assertEqual((r["result"], [v["file"] for v in r["violations"]]),
                         ("fail", ["src/test/java/com/example/other/B.java"]))


class DropCandidateTest(unittest.TestCase):
    def test_drop_candidates_from_scoring_table(self):
        t = sr.parse_scoring_items((Path(run_task.load_config()["task_dir"]) / "scoring-items.md")
                                   .read_text(encoding="utf-8"))
        drop = sorted(k for k, v in t.items() if v["drop_candidate"])
        self.assertEqual(drop, ["D0-12", "D2-04", "D2-05", "D4-07", "D5-04", "D6-07", "D6-08", "D7-08"])

    def test_items_json_fragile_agrees(self):
        """items.json의 fragile은 사람이 읽는 사본 — 채점은 scoring-items.md만 읽지만 둘이 어긋나면 알린다."""
        cfg = run_task.load_config()
        t = sr.parse_scoring_items((Path(cfg["task_dir"]) / "scoring-items.md").read_text(encoding="utf-8"))
        items = json.loads(run_task.resolve(cfg["hidden"]["items_file"]).read_text(encoding="utf-8"))["items"]
        self.assertEqual({x["id"] for x in items if "●" in (x.get("fragile") or "")},
                         {x["id"] for x in items if t[x["id"]]["drop_candidate"]})


class GitSnapshotTest(unittest.TestCase):
    """턴 끝 HEAD로 전제·이력 재작성·K2·K7a(S8)를 판정."""

    def setUp(self):
        self.w = Path(tempfile.mkdtemp())
        write(self.w, "src/main/resources/db/migration/.gitkeep", "")
        git(self.w, "init", "-q", "-b", "main")
        git(self.w, "add", "-A")
        git(self.w, "commit", "-q", "-m", "skeleton", date=T0)
        self.skel = head(self.w)

    def commit(self, msg, date, files):
        for rel, text in files.items():
            write(self.w, rel, text)
        git(self.w, "add", "-A")
        git(self.w, "commit", "-q", "-m", msg, date=date)
        return head(self.w)

    def run_json(self, heads, steps=None, diffs=None):
        steps = steps or [f"S{k}" for k in range(len(heads))]
        return {"skeleton_commit": self.skel, "turns": [
            {"index": k, "step": steps[k], "started_at": T0 + 100 * k, "git_head": h,
             "tracked_diff_sha256": (diffs or [None] * len(heads))[k]} for k, h in enumerate(heads)]}

    def test_premise_uses_turn_head_not_commit_prefix(self):
        yml = {"src/main/resources/application.yml": YML_S3,
               "src/main/kotlin/A.kt": 'val q = "SELECT pg_try_advisory_xact_lock(1)"\n'}
        h = [self.commit(f"단계 {k}", T0 + 100 * k + 10, {f"f{k}.txt": "x"}) for k in range(3)]
        h.append(self.commit("접두 없는 메시지", T0 + 310, yml))
        items = sr.premise_items(self.w, self.run_json(h))
        self.assertEqual(items["D3-00"]["result"], "pass")
        self.assertEqual(items["D3-00"]["commit"], h[3])
        items = sr.premise_items(self.w, self.run_json(h[:3] + [h[2]]))  # S3 턴에 커밋 없음
        self.assertEqual(items["D3-00"]["result"], "unscorable")
        self.assertIn("새 커밋 없음", items["D3-00"]["reason"])

    def test_history_rewrite_flagged_and_k2_sees_snapshot_change(self):
        mig = "src/main/resources/db/migration/V1__init.sql"
        h0 = self.commit("[S0] a", T0 + 10, {mig: "CREATE TABLE a (id INT);\n"})
        h1 = self.commit("[S1] b", T0 + 110, {"x.txt": "1"})
        # S2에서 앞 커밋들을 하나로 합치며 V1을 고친다(amend·rebase 흉내) → 최종 이력에는 V1의 A만 남는다
        git(self.w, "reset", "-q", "--soft", self.skel)
        write(self.w, mig, "CREATE TABLE a (id INT, b INT);\n")
        git(self.w, "add", "-A")
        git(self.w, "commit", "-q", "-m", "[S2] 합침", date=T0 + 210)
        h2 = head(self.w)
        run = self.run_json([h0, h1, h2])
        self.assertEqual(sr.history_check(self.w, run), {"rewritten_turns": [0, 1], "missing_turns": []})
        k2 = sr.k2(self.w, run)
        self.assertEqual(k2["result"], "fail")
        self.assertEqual([(v["status"], v["where"]) for v in k2["violations"]], [("M", "turn 2")])

    def test_k7a_s8_requires_commit_only_when_tracked_files_changed(self):
        h = [self.commit(f"[S{k}] x", T0 + 100 * k + 10, {f"f{k}.txt": "x"}) for k in range(8)]
        steps = [f"S{k}" for k in range(9)]
        same = self.run_json(h + [h[7]], steps, diffs=["d"] * 9)
        a, _ = sr.k7(self.w, same)
        self.assertEqual((a["result"], a["steps_requiring_commit"]), ("pass", 8))
        changed = self.run_json(h + [h[7]], steps, diffs=["d"] * 8 + ["e"])
        a, _ = sr.k7(self.w, changed)
        self.assertEqual((a["result"], a["steps_without_commit"], a["steps_requiring_commit"]), ("fail", [8], 9))


class HiddenMainBuildTest(unittest.TestCase):
    """채점 컨테이너 대신 가짜 실행 함수 — 메인 빌드 실패면 H 항목 fail, 숨긴 테스트만 컴파일 실패면 unscorable."""

    COMPILE_ERR = "e: file:///work/src/main/kotlin/A.kt:3:5 Unresolved reference 'x'.\n" \
                  "Execution failed for task ':compileKotlin'.\n> Compilation error. See log for more details\n"
    RESOLVE_ERR = "Execution failed for task ':compileKotlin'.\n> Could not resolve all files for configuration " \
                  "':compileClasspath'.\n   > Could not resolve org.example:lib:1.0.\n      > Could not GET 'https://repo'.\n"

    def hidden(self, main_rc, test_rc=1, main_log=COMPILE_ERR, test_log=""):
        cfg = run_task.load_config()
        work, rdir = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        write(work, "build.gradle.kts", BUILD)

        def fake_executor(*_):
            def execute(cmd, cwd, log, timeout):
                main = cmd == cfg["hidden"]["main_build_command"]
                log.write((main_log if main else test_log).encode())
                return main_rc if main else test_rc  # JUnit XML 없음
            return execute
        orig, sr.container_executor = sr.container_executor, fake_executor
        try:
            return sr.hidden_items(cfg, work, rdir, False, docker=None, stack=object())
        finally:
            sr.container_executor = orig

    def test_main_build_failure_is_fail(self):
        items, meta = self.hidden(main_rc=1)
        self.assertEqual(meta["reason"], "main_build_failed")
        self.assertEqual((items["D0-02"]["result"], items["D0-02"]["reason"]), ("fail", "main_build_failed"))
        self.assertEqual(items["D0-12"]["result"], "unscorable")  # 테스트 없는 항목
        self.assertEqual(items["D6-04"]["alias_of"], "D0-13")

    def test_hidden_only_build_failure_is_unscorable(self):
        items, meta = self.hidden(main_rc=0)
        self.assertEqual(meta["reason"], "hidden_build_failed_or_no_tests")
        self.assertEqual(items["D0-02"]["result"], "unscorable")

    def test_main_build_environment_failure_is_unscorable_and_flagged(self):
        items, meta = self.hidden(main_rc=1, main_log=self.RESOLVE_ERR)
        self.assertEqual((meta["reason"], meta["main_build"]["failure_kind"]), ("scoring_env_main_build", "environment"))
        self.assertEqual((items["D0-02"]["result"], items["D0-02"]["reason"]), ("unscorable", "scoring_env_main_build"))
        self.assertIn("scoring_env_main_build", meta["operator_flag"])
        items, meta = self.hidden(main_rc=125, main_log="")  # docker run이 컨테이너를 시작하지 못함
        self.assertEqual(items["D0-02"]["result"], "unscorable")

    def test_main_build_unclassified_failure_is_unscorable_and_flagged(self):
        items, meta = self.hidden(main_rc=1, main_log="FAILURE: Build failed with an exception.\n* What went wrong:\nboom\n")
        self.assertEqual((items["D0-02"]["result"], meta["reason"]), ("unscorable", "main_build_failed_unclassified"))
        self.assertIn("operator_flag", meta)

    def test_main_build_compile_error_is_not_flagged(self):
        items, meta = self.hidden(main_rc=1)
        self.assertEqual(items["D0-02"]["result"], "fail")
        self.assertNotIn("operator_flag", meta)

    def test_hidden_phase_resolve_failure_is_flagged(self):
        items, meta = self.hidden(main_rc=0, main_log="", test_log=self.RESOLVE_ERR.replace("compileClasspath",
                                                                                           "testRuntimeClasspath"))
        self.assertEqual((items["D0-02"]["result"], meta["reason"]), ("unscorable", "scoring_env_hidden_build"))
        self.assertIn("operator_flag", meta)



class ScoringConcurrencyTest(unittest.TestCase):
    """채점도 동시 실행 사전 확인(가짜 docker)."""

    def cfg(self, base):
        cfg = run_task.load_config()
        fd = Path(run_task.ROOT) / "tools" / "tests" / "fake_docker.py"
        os.chmod(fd, 0o755)
        cfg["docker_bin"] = str(fd)
        return cfg

    def test_refuses_when_other_experiment_running(self):
        base = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(base / "d"), "FAKE_DOCKER_PS": "payment-claude-1"}):
            with self.assertRaises(SystemExit):
                sr.open_stack(self.cfg(base), True)
            calls = (base / "d" / "calls.jsonl").read_text()
            self.assertNotIn('"create"', calls)
            docker, stack, err, info = sr.open_stack(self.cfg(base), True, allow_concurrent=True)
            self.assertIsNone(err)
            stack.close()
            self.assertEqual(stack.cleanup_errors, [])

    def test_refuses_when_this_experiment_running(self):
        base = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(base / "d")}):
            cfg = self.cfg(base)
            d = sr.containers.Docker.from_cfg(cfg)
            d.run("run", "-d", "--name", "cx-other-db", "--label", sr.LABEL, "postgres:16")
            with self.assertRaises(SystemExit):
                sr.open_stack(cfg, True, allow_concurrent=True)


if __name__ == "__main__":
    unittest.main()
