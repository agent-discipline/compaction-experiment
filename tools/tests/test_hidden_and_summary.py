import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import TOOLS  # noqa: F401

import hidden_tests as ht
import summarize

XML = """<?xml version="1.0"?>
<testsuite name="com.example.booking.hidden.HoldTest">
  <testcase name="선점 201()" classname="com.example.booking.hidden.HoldTest"/>
  <testcase name="실패 코드()" classname="com.example.booking.hidden.HoldTest">
    <failure message="expected 409" type="AssertionError">trace</failure>
  </testcase>
  <testcase name="시드()" classname="com.example.booking.hidden.SeedTest">
    <error message="UNSCORABLE seed failed [sqlstate=23502]" type="IllegalStateException"/>
  </testcase>
  <testcase name="가정()" classname="com.example.booking.hidden.SeedTest$Inner">
    <skipped message="assumption"/>
  </testcase>
</testsuite>
"""


class HiddenTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "TEST-x.xml").write_text(XML, encoding="utf-8")
        self.cases = ht.parse_junit(self.d, "^UNSCORABLE")

    def test_parse_statuses(self):
        self.assertEqual([c["status"] for c in self.cases], ["pass", "fail", "unscorable", "unscorable"])

    def test_item_mapping(self):
        items = {"A": ["com.example.booking.hidden.HoldTest#선점 201"],
                 "B": ["com.example.booking.hidden.HoldTest"],
                 "C": ["com.example.booking.hidden.SeedTest"],
                 "D": ["com.example.booking.hidden.Nope"]}
        r = ht.item_results(items, self.cases)
        self.assertEqual({k: v["result"] for k, v in r.items()},
                         {"A": "pass", "B": "fail", "C": "unscorable", "D": "not_run"})
        self.assertEqual(len(r["C"]["tests"]), 2)  # 중첩 클래스 포함

    def test_load_items_formats(self):
        for data in ({"X": ["a"]}, {"items": {"X": ["a"]}}, {"items": [{"id": "X", "tests": ["a"]}]},
                     [{"id": "X", "tests": ["a"]}]):
            p = self.d / "i.json"
            p.write_text(json.dumps(data))
            self.assertEqual(ht.load_items(p)[0], {"X": ["a"]})
        p.write_text(json.dumps({"items": [
            {"id": "D0-13", "class": "C", "display_name": "D0-13 x (y) z"},
            {"id": "D6-04", "class": "C", "display_name": "D0-13 x (y) z", "alias_of": "D0-13"},
            {"id": "D0-12", "class": None, "display_name": None, "no_test": "없음"}]}))
        m, meta = ht.load_items(p)
        self.assertEqual(m, {"D0-13": ["C#D0-13 x (y) z"], "D6-04": ["C#D0-13 x (y) z"], "D0-12": []})
        self.assertEqual(meta["D6-04"]["alias_of"], "D0-13")
        r = ht.item_results(m, [{"classname": "C", "name": "D0-13 x (y) z", "status": "pass", "message": ""}])
        self.assertEqual({k: v["result"] for k, v in r.items()}, {"D0-13": "pass", "D6-04": "pass", "D0-12": "unscorable"})
        p.write_text(json.dumps({"X": "a"}))
        with self.assertRaises(ValueError):
            ht.load_items(p)


BASE_RUN = {"messages_sha256_all": "M", "image_id": "img", "skeleton_commit": "sk", "cli_version": "2.1.326",
            "limits": {"turn_timeout": 5400}, "container_limits": {"cpus": "4"},
            "container_env": {"HOME": "/home/agent", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "x"}, "outbox_config_sha256": None}
SETTINGS = {"control": ("S-c", "1000000"), "compact": ("S-c", "100000"), "outbox": ("S-o", "100000")}


def item(res, after=0, unlocated=0, **kw):
    return {"result": res, "compactions_after_delivery": after, "compactions_unlocated": unlocated, **kw}


def make_run(base, run_id, cond, items=None, model="m", complete=True, invalid=(), failures=(), turns=(), **over):
    d = Path(base) / run_id
    d.mkdir(parents=True)
    run = dict(BASE_RUN, run_id=run_id, model=model, condition=cond, measurement_complete=complete,
               invalid=bool(invalid), invalid_reasons=list(invalid), failure_reasons=list(failures),
               settings_sha256=SETTINGS[cond][0], compact_window_env=SETTINGS[cond][1],
               totals={"tokens": 10, "cost_usd": None}, turns=list(turns), **over)
    (d / "run.json").write_text(json.dumps(run), encoding="utf-8")
    if items is not None:
        (d / "score.json").write_text(json.dumps({
            "run_id": run_id, "model": model, "condition": cond,
            "run_sha256": hashlib.sha256((d / "run.json").read_bytes()).hexdigest(), "items": items, "compactions": 0 if cond == "control" else 2,
            "boundaries": [] if cond == "control" else [{"coord": 1.0, "mid_turn_phase": "before_first_output"}, {"coord": None}],
            "injected_chars": 100 if cond == "outbox" else None, "injected_tokens": None,
            "history": {"rewritten_turns": [], "missing_turns": []}}), encoding="utf-8")
    return d


class SummaryTest(unittest.TestCase):
    def setUp(self):
        b = self.base = Path(tempfile.mkdtemp())
        drop = item("fail", drop_candidate=True)
        alias = item("fail", alias_of="I0")
        self.dirs = [
            make_run(b, "c1", "control", {"I0": item("pass"), "I1": item("fail"), "I2": item("pass"),
                                          "D": drop, "A": alias}),
            make_run(b, "c2", "control", {"I0": item("fail"), "I1": item("fail"), "I2": item("pass")}),
            make_run(b, "k1", "compact", {"I0": item("fail", 1), "I1": item("pass", 2), "I2": item("pass"),
                                          "I3": item("fail", 0, 1), "D": item("fail", 1, drop_candidate=True),
                                          "A": item("pass", 1, alias_of="I0"), "U": item("unscorable", 1)},
                     turns=[{"reread": {"session_log": [{"line": 3}], "memo": []}},
                            {"reread": {"memo": [{"line": 9}]}}]),
            make_run(b, "k2", "compact", None),                                    # 미채점
            make_run(b, "k3", "compact", {"I0": item("fail", 1)}, invalid=["no_compaction"]),
            make_run(b, "k4", "compact", {"I0": item("fail", 1)}, complete=False),  # 중단
            make_run(b, "k5", "compact", {"I0": item("pass", 1)}, messages_sha256_all="OTHER"),
            make_run(b, "o1", "outbox", {"I0": item("pass", 1), "I1": item("pass", 1)},
                     failures=["secret_leak"], outbox_config_sha256="cfg"),
        ]
        self.res = summarize.summarize(summarize.load(self.dirs))

    def row(self, cond):
        return next(r for r in self.res["rows"] if r["condition"] == cond)

    def test_run_states_counted_from_run_json(self):
        k = self.row("compact")
        self.assertEqual((k["runs"], k["measurement_complete"], k["aborted"], k["unscored"], k["invalid"],
                          k["input_mismatch"], k["included"]), (5, 4, 1, 1, 1, 1, 1))
        self.assertEqual((k["reread_runs"], k["memo_runs"]), (1, 1))
        o = self.row("outbox")
        self.assertEqual((o["included"], o["ops_problems"], o["secret_leak"]), (1, 1, 1))  # 토큰 검출이어도 측정은 유효

    def test_input_mismatch_by_majority(self):
        ex = {x["run_id"]: x for x in self.res["excluded"]}
        self.assertEqual(ex["k5"]["input_mismatch"], ["messages_sha256_all"])
        self.assertNotIn("o1", [r for r, x in ex.items() if x["input_mismatch"]])  # 아웃박스 설정은 조건 안에서만 비교
        recs = summarize.load([make_run(self.base, "x1", "control", {}), make_run(self.base, "x2", "control", {},
                                                                               image_id="img2")])
        self.assertEqual(sorted(summarize.input_mismatches(recs)), ["x1", "x2"])  # 최다 값이 없으면 둘 다
        on, off = {"settings": {}, "env": {}}, {"settings": {"autoMemoryEnabled": False},
                                                 "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}}
        recs = summarize.load([make_run(self.base, "y1", "control", {}, auto_memory=off),
                               make_run(self.base, "y2", "compact", {}, auto_memory=off),
                               make_run(self.base, "y3", "outbox", {}, auto_memory=on)])
        self.assertEqual(summarize.input_mismatches(recs), {"y3": ["auto_memory"]})  # 자동 메모리 설정은 조건 사이에서도 대조

    def test_rates_exclude_alias_drop_and_unjudged(self):
        c = self.row("control")
        self.assertEqual((c["violation_rate"], c["judged"]), (3 / 6, 6))
        self.assertEqual((c["drop_candidate_rate"], c["drop_candidate_judged"]), (1.0, 1))
        self.assertEqual(c["run_violation_rates"], [1 / 3, 2 / 3])  # 별칭(A)·버릴 후보(D) 제외
        k = self.row("compact")
        self.assertEqual((k["violation_rate"], k["judged"], k["not_judged"]), (0.5, 4, {"unscorable": 1}))
        self.assertEqual(k["unlocated_boundaries"], 1)
        self.assertEqual(k["prompt_adjacent_boundaries"], 1)  # 턴 도중 중 프롬프트 직후(첫 출력 전) 경계 — 기록만
        self.assertIn("프롬프트 직후 경계", summarize.markdown(self.res))

    def test_primary_metric_pairs_same_items_with_controls(self):
        pair = next(x for x in self.res["pairs"] if x["run_id"] == "k1")
        self.assertEqual((pair["items"], pair["unknown_items"], pair["judged"], pair["rate"]), (3, 1, 2, 0.5))  # U는 판정 제외
        self.assertEqual((pair["controls"], pair["control_rate_mean"], pair["control_rate_span"]), (2, 0.75, (0.5, 1.0)))
        self.assertAlmostEqual(pair["diff"], -0.25)
        self.assertEqual((pair["compared_items"], pair["left_out_items"]), ([2, 2], [1, 1]))  # U는 양쪽에서 뺌
        o = next(x for x in self.res["pairs"] if x["run_id"] == "o1")
        self.assertEqual((o["rate"], o["control_rate_mean"]), (0.0, 0.75))

    def test_unscorable_items_left_out_on_both_sides(self):
        b = Path(tempfile.mkdtemp())
        recs = summarize.load([
            make_run(b, "t", "compact", {"X": item("fail", 1), "Y": item("pass", 1), "Z": item("unscorable", 1)}),
            make_run(b, "cA", "control", {"X": item("unscorable"), "Y": item("fail"), "Z": item("pass")}),
            make_run(b, "cB", "control", {"X": item("not_run"), "Y": item("unscorable"), "Z": item("fail")})])
        pair = summarize.summarize(recs)["pairs"][0]
        # cA와는 Y 하나만 비교(X는 대조군에서, Z는 컴팩트 실행에서 판정 불가) · cB는 비교 항목이 없어 대조군 수에서 빠짐
        self.assertEqual((pair["controls"], pair["controls_without_items"]), (1, 1))
        self.assertEqual((pair["compared_items"], pair["left_out_items"]), ([1], [2]))
        self.assertEqual((pair["rate"], pair["control_rate_mean"], pair["diff"]), (0.0, 1.0, -1.0))
        self.assertEqual(pair["own_rate"], 0.5)
        md = summarize.markdown(summarize.summarize(recs))
        self.assertIn("| t | 3 | 0 | 2 | 1(2) · 비교 항목 없는 대조군 1 | 0.0% | (1) 100.0% [100.0~100.0%] | -100.0%p |", md)

    def test_stale_score_is_excluded_loudly(self):
        b = self.base
        a = make_run(b, "s1", "control", {"I0": item("pass")})
        other = make_run(b, "s2", "control", {"I0": item("fail")})
        (a / "score.json").write_text((other / "score.json").read_text())        # 다른 실행의 채점이 남음
        c = make_run(b, "s3", "control", {"I0": item("pass")})
        run = json.loads((c / "run.json").read_text())
        run["turns"] = [{"index": 0}]                                            # 채점 뒤 run.json이 바뀜
        (c / "run.json").write_text(json.dumps(run))
        res = summarize.summarize(summarize.load([a, other, c]))
        ex = {x["run_id"]: x for x in res["excluded"]}
        self.assertEqual(ex["s1"]["score_mismatch"], ["run_id", "run_sha256"])
        self.assertEqual(ex["s3"]["score_mismatch"], ["run_sha256"])
        self.assertNotIn("s2", ex)
        row = next(r for r in res["rows"] if r["condition"] == "control")
        self.assertEqual((row["included"], row["score_mismatch"], row["judged"]), (1, 2, 1))
        md = summarize.markdown(res)
        self.assertIn("- s1: 채점 파일이 이 실행의 것이 아님 run_id, run_sha256 — 다시 채점", md)

    def test_per_item_split_by_compaction(self):
        cell = self.res["per_item"]["I0"]["cells"]
        self.assertEqual(cell[("m", "compact")]["after"], [1, 1])
        self.assertEqual(cell[("m", "control")]["none"], [1, 2])
        self.assertEqual(self.res["per_item"]["I3"]["cells"][("m", "compact")]["unknown"], [1, 1])
        self.assertNotIn("A", self.res["per_item"])
        self.assertTrue(self.res["per_item"]["D"]["drop_candidate"])

    def test_markdown_and_cli(self):
        md = summarize.markdown(self.res)
        self.assertIn("| m | compact | 5 | 4 | 1 | 1 | 0 | 1 | 1 | 1 | 0 | 0 | 0 | 1 | 1 |", md)
        self.assertIn("- k2: 미채점", md)
        self.assertIn("| k1 | 3 | 1 | 2 | 2(1), 2(1) | 50.0% | (2) 75.0% [50.0~100.0%] | -25.0%p |", md)
        self.assertIn("| I0 | 뒤 1/1 | 없음 1/2 |", md.replace(" | 뒤 0/0", ""))
        self.assertIn("| D ● |", md)
        self.assertIn("미측정", md)
        out = self.base / "s.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(summarize.main(["--runs-dir", str(self.base), "--json", str(out)]), 0)
        self.assertEqual(len(json.loads(out.read_text())["pairs"]), 2)


if __name__ == "__main__":
    unittest.main()
