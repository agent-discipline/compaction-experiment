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


def score(cond, results, compactions_after=1, invalid=False, status="complete"):
    return {"model": "m", "condition": cond, "status": status, "invalid": invalid, "compactions": 2,
            "injected_chars": 100 if cond == "outbox" else None, "totals": {"tokens": 10},
            "items": {f"I{k}": {"result": r, "compactions_after_delivery": compactions_after}
                      for k, r in enumerate(results)}}


class SummaryTest(unittest.TestCase):
    def test_rates_exclude_invalid_and_unjudged(self):
        scores = [score("compact", ["pass", "fail", "unscorable", "premise_unmet"]),
                  score("compact", ["fail", "fail"], invalid=True),
                  score("compact", ["fail"], status="aborted"),
                  score("control", ["pass", "pass"], compactions_after=0)]
        rows, per_item = summarize.summarize(scores)
        c = next(r for r in rows if r["condition"] == "compact")
        self.assertEqual((c["runs"], c["valid_complete"], c["invalid"], c["aborted"]), (3, 1, 1, 1))
        self.assertEqual((c["violation_rate"], c["judged"]), (0.5, 2))
        self.assertEqual(c["not_judged"], {"unscorable": 1, "premise_unmet": 1})
        k = next(r for r in rows if r["condition"] == "control")
        self.assertIsNone(k["violation_rate_after_compaction"])
        self.assertEqual(per_item["I1"][("m", "compact")], [1, 1])
        self.assertIn("| m | compact | 3 | 1 | 1 | 1 | 50.0% (2)", summarize.markdown(rows, per_item))


if __name__ == "__main__":
    unittest.main()
