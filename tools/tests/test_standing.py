"""standing 조건 — 고정 지시 훅 설정·스크립트·기록·무효 판정·집계, 그리고 다른 조건이 그대로인지."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TOOLS

import run_task as rt
import summarize
import test_hidden_and_summary as ths
import test_run_task

STANDING = TOOLS / "standing"
# 계약(명세 "후속 조건 standing 추가") — 하네스 v4.5 훅이 내는 문구 그대로
LINE = "[고정 지시] 사용자에게 하는 응답은 항상 한국어로 쓴다(코드, 명령, 인용은 원문 그대로)."
# 본 실행(2026-11-18~19 Haiku 9회) run.json의 settings_sha256 — 기존 조건의 설정은 바이트 단위로 같아야 한다
MAIN_SETTINGS_SHA = {"control": "0c2001d3a27e36c011e386d9db13a8af63ea8c66d78d210e60a34fe2d5c55429",
                     "compact": "0c2001d3a27e36c011e386d9db13a8af63ea8c66d78d210e60a34fe2d5c55429",
                     "outbox": "ed6495b368af8151807cce6ac0ed760e1a0e690b9a3490abf7d633123f7b404f"}


def hook(data, stdin, cfg=STANDING / "config.json"):
    return subprocess.run([sys.executable, "-I", str(STANDING / "standing.py"), str(data), str(cfg)],
                          input=stdin, capture_output=True, text=True)


def rows(path):
    p = Path(path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


class StandingSettingsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rt.load_config()

    def test_existing_conditions_settings_unchanged(self):
        for cond, sha in MAIN_SETTINGS_SHA.items():
            text = rt.settings_text(rt.settings_for(cond, self.cfg))
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), sha, cond)
            self.assertNotIn("standing", text, cond)

    def test_standing_is_compact_plus_two_hooks(self):
        k, s = rt.settings_for("compact", self.cfg), rt.settings_for("standing", self.cfg)
        self.assertEqual({x: v for x, v in s.items() if x != "hooks"}, {x: v for x, v in k.items() if x != "hooks"})
        self.assertEqual({e: s["hooks"][e] for e in k["hooks"]}, k["hooks"])
        cmd = "/usr/bin/python3 -I /cx/hooks/standing.py /cx/hookdata /cx/hooks/standing-config.json"
        group = [{"hooks": [{"type": "command", "timeout": 30, "command": cmd}]}]  # 매처 없음 = 모든 source
        self.assertEqual({e: s["hooks"][e] for e in set(s["hooks"]) - set(k["hooks"])},
                         {"UserPromptSubmit": group, "SessionStart": group})
        self.assertEqual(rt.container_env(self.cfg, "standing"), rt.container_env(self.cfg, "compact"))
        self.assertIs(s["autoMemoryEnabled"], False)

    def test_config_line_matches_contract(self):
        c = json.loads((STANDING / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(c["line"], LINE)
        self.assertEqual(c["events"], ["UserPromptSubmit", "SessionStart"])

    def test_staged_hooks(self):
        d = Path(tempfile.mkdtemp())
        rt.stage_hooks(d / "s", "standing")
        rt.stage_hooks(d / "k", "compact")
        self.assertEqual(sorted(p.name for p in (d / "s").iterdir()),
                         ["compact_log.py", "empty-mcp.json", "standing-config.json", "standing.py"])
        self.assertEqual(sorted(p.name for p in (d / "k").iterdir()), ["compact_log.py", "empty-mcp.json"])
        self.assertEqual((d / "s" / "standing-config.json").read_bytes(), (STANDING / "config.json").read_bytes())


class StandingHookTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()) / "hookdata"

    def test_output_is_exact_line_and_logged(self):
        p = hook(self.d, json.dumps({"session_id": "s-1", "hook_event_name": "SessionStart", "source": "compact"}))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, LINE + "\n", ""))
        p = hook(self.d, json.dumps({"session_id": "s-1", "hook_event_name": "UserPromptSubmit", "prompt": "안녕"}))
        self.assertEqual(p.stdout, LINE + "\n")
        r = rows(self.d / "standing-log.jsonl")
        self.assertEqual([{k: v for k, v in x.items() if k != "at"} for x in r],
                         [{"event": "SessionStart", "source": "compact", "session_id": "s-1"},
                          {"event": "UserPromptSubmit", "source": None, "session_id": "s-1"}])
        self.assertEqual([list(x) for x in r], [["at", "event", "source", "session_id"]] * 2)
        self.assertFalse((self.d / "hook-errors.jsonl").exists())

    def test_bad_input_still_injects_and_records_error(self):
        p = hook(self.d, "{not json")
        self.assertEqual((p.returncode, p.stdout), (0, LINE + "\n"))
        self.assertEqual([x["event"] for x in rows(self.d / "standing-log.jsonl")], [None])
        self.assertEqual([x["hook"] for x in rows(self.d / "hook-errors.jsonl")], ["standing"])

    def test_missing_config_no_output_error_logged(self):
        p = hook(self.d, "{}", cfg=self.d.parent / "none.json")
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertFalse((self.d / "standing-log.jsonl").exists())
        self.assertEqual([x["hook"] for x in rows(self.d / "hook-errors.jsonl")], ["standing"])


def ts(sec):
    return f"2026-11-19T00:00:{sec:02d}+00:00"


def srow(event, source=None, sec=0):
    return {"at": ts(sec), "event": event, "source": source, "session_id": "s"}


class StandingValidityTest(unittest.TestCase):
    B = [{"turn": 1}]
    LOG = [{"event": "PreCompact", "ts": ts(10)}, {"event": "PostCompact", "ts": ts(11)}]

    def test_counts(self):
        r = [srow("SessionStart", "startup"), srow("UserPromptSubmit"), srow("SessionStart", "resume"),
             srow("SessionStart", "compact"), srow("UserPromptSubmit"), srow(None)]
        self.assertEqual(rt.standing_counts(r), {"UserPromptSubmit": 2, "SessionStart": {"compact": 1, "resume": 1,
                                                                                         "startup": 1},
                                                 "SessionStart_total": 3, "other": 1, "total": 6})

    def test_invalid_reasons(self):
        good = [srow("UserPromptSubmit", sec=1), srow("SessionStart", "compact", 12), srow("UserPromptSubmit", sec=13)]
        inv = rt.invalid_reasons
        self.assertEqual(inv("standing", self.B, [], False, [], self.LOG, standing_log=good, n_turns=2), [])
        self.assertEqual(inv("standing", self.B, [], False, [], self.LOG, standing_log=good, n_turns=3),
                         ["standing_prompt_injection_count_mismatch"])
        early = [good[0], srow("SessionStart", "compact", 5), good[2]]  # 경계 앞의 compact 주입은 그 경계의 것이 아님
        self.assertEqual(inv("standing", self.B, [], False, [], self.LOG, standing_log=early, n_turns=2),
                         ["standing_boundary_without_injection"])
        self.assertEqual(inv("standing", self.B, [], False, [], self.LOG, standing_log=[good[0], good[2]], n_turns=2),
                         ["standing_compact_injection_count_mismatch", "standing_boundary_without_injection"])
        self.assertEqual(inv("standing", self.B, [], False, [], self.LOG, [{"hook": "standing"}], standing_log=good,
                             n_turns=2), ["standing_hook_error"])
        self.assertIn("standing_log_unreadable", inv("standing", self.B, [], False, [], self.LOG, n_turns=0))
        self.assertEqual(inv("standing", [], [], False, [], [], standing_log=[], n_turns=0), ["no_compaction"])
        # 다른 조건은 standing 인자를 무시한다
        self.assertEqual(inv("compact", self.B, [], False, [], self.LOG, [{"hook": "standing"}], standing_log=[],
                             n_turns=9), [])


class StandingRunTest(unittest.TestCase):
    run_fake = test_run_task.RunLoopTest.run_fake

    def test_standing_run_end_to_end(self):
        rc, run, rdir, base = self.run_fake("standing")
        self.assertEqual((rc, run["status"], run["invalid"], run["invalid_reasons"]), (0, "complete", False, []))
        self.assertEqual(run["compact_window_env"], "100000")
        self.assertEqual(run["standing_config"], {"line": LINE, "events": ["UserPromptSubmit", "SessionStart"]})
        self.assertEqual(run["standing_config_sha256"],
                         hashlib.sha256((STANDING / "config.json").read_bytes()).hexdigest())
        self.assertEqual(run["injections"], [])                   # outbox 주입은 쓰지 않는다
        self.assertFalse((rdir / "hookdata" / "outbox").exists())
        per_turn = [t["standing_injections"] for t in run["turns"]]
        self.assertEqual([(t["UserPromptSubmit"], t["SessionStart"]) for t in per_turn],
                         [(1, {"startup": 1}), (1, {"compact": 1, "resume": 1}), (1, {"compact": 1, "resume": 1})])
        self.assertEqual(run["standing_counts"], {"UserPromptSubmit": 3, "SessionStart": {"compact": 2, "resume": 2,
                                                                                           "startup": 1},
                                                  "SessionStart_total": 5, "other": 0, "total": 8})
        self.assertEqual(len(run["standing_injections"]), 8)
        self.assertEqual(run["standing_boundary_injections"], [{"boundary": 0, "injection": 0},
                                                               {"boundary": 1, "injection": 1}])
        self.assertEqual(rows(rdir / "hookdata" / "standing-log.jsonl"), run["standing_injections"])
        meta = json.loads((rdir / "turns" / "01" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["standing_injections"], per_turn[1])
        st = (rdir / "settings.json").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(st.encode()).hexdigest(), run["settings_sha256"])
        self.assertIn("standing.py", st)

    def test_other_conditions_have_no_standing_keys(self):
        for cond in ("control", "compact", "outbox"):
            rc, run, rdir, base = self.run_fake(cond)
            self.assertFalse([k for k in run if k.startswith("standing")], cond)
            self.assertFalse([k for t in run["turns"] for k in t if k.startswith("standing")], cond)
            self.assertFalse((rdir / "hookdata" / "standing-log.jsonl").exists(), cond)
            self.assertEqual(run["settings_sha256"], MAIN_SETTINGS_SHA[cond], cond)
            self.assertFalse(run["invalid"], run["invalid_reasons"])

    def test_standing_hook_error_invalidates(self):
        """설정 파일이 깨지면 주입이 없다 → 훅 오류·주입 수 불일치로 무효."""
        orig = rt.stage_hooks

        def broken(dest, condition):
            orig(dest, condition)
            if condition == "standing":
                (dest / "standing-config.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(rt, "stage_hooks", broken):
            rc, run, *_ = self.run_fake("standing")
        self.assertTrue(run["invalid"])
        self.assertIn("standing_hook_error", run["invalid_reasons"])
        self.assertIn("standing_prompt_injection_count_mismatch", run["invalid_reasons"])
        self.assertEqual(run["standing_counts"]["total"], 0)


class StandingSummaryTest(unittest.TestCase):
    def test_standing_in_tables_and_primary(self):
        b = Path(tempfile.mkdtemp())
        counts = {"UserPromptSubmit": 9, "SessionStart": {"compact": 12, "resume": 8, "startup": 1},
                  "SessionStart_total": 21, "other": 0, "total": 30}
        with mock.patch.dict(ths.SETTINGS, standing=("S-s", "100000")):
            dirs = [ths.make_run(b, "c1", "control", {"I0": ths.item("pass"), "I1": ths.item("pass"),
                                                      "K8": ths.item("pass")}, rep=1),
                    ths.make_run(b, "k1", "compact", {"I0": ths.item("fail", 1), "I1": ths.item("pass"),
                                                      "K8": ths.item("fail", 1)}, rep=1),
                    ths.make_run(b, "s1", "standing", {"I0": ths.item("fail", 1), "I1": ths.item("pass", 1),
                                                       "K8": ths.item("pass", 1)}, rep=1, injections=[],
                                 boundaries=[{}, {}], standing_config_sha256="cfg", standing_counts=counts)]
        res = summarize.summarize(summarize.load(dirs))
        self.assertEqual(res["excluded"], [])
        self.assertIn(("m", "standing"), [(r["model"], r["condition"]) for r in res["rows"]])
        prim = {x["condition"]: x for x in res["primary"]}
        self.assertEqual(set(prim), {"compact", "standing"})
        self.assertAlmostEqual(prim["standing"]["diff_mean"], 1 / 3)  # I0 fail·I1·K8 pass vs 대조군 셋 다 pass
        runs = {x["run_id"]: x for x in res["runs"]}
        self.assertEqual(runs["s1"]["standing_counts"], counts)
        self.assertNotIn("standing_counts", runs["k1"])
        self.assertEqual(res["k_items"]["m"]["standing"]["K8"], [(1, "pass")])
        md = summarize.markdown(res)
        self.assertIn("### standing 주입 횟수", md)
        self.assertIn("| s1 | 1 | 예 | 2 | 9 | 12 | resume 8, startup 1 | 30 |", md)
        self.assertIn("| m | standing | 1 | 1 |", md)

    def test_no_standing_section_without_standing_runs(self):
        b = Path(tempfile.mkdtemp())
        dirs = [ths.make_run(b, "c1", "control", {"I0": ths.item("pass")}),
                ths.make_run(b, "k1", "compact", {"I0": ths.item("fail", 1)})]
        md = summarize.markdown(summarize.summarize(summarize.load(dirs)))
        self.assertNotIn("standing", md)

    def test_standing_config_compared_within_condition(self):
        b = Path(tempfile.mkdtemp())
        with mock.patch.dict(ths.SETTINGS, standing=("S-s", "100000")):
            dirs = [ths.make_run(b, f"s{i}", "standing", {"I0": ths.item("pass", 1)}, standing_config_sha256=v)
                    for i, v in enumerate(("cfg", "cfg", "other"))]
        res = summarize.summarize(summarize.load(dirs))
        self.assertEqual({x["run_id"]: x["input_mismatch"] for x in res["excluded"]}, {"s2": ["standing_config_sha256"]})


if __name__ == "__main__":
    unittest.main()
