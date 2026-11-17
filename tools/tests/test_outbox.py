import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import TOOLS

OUTBOX = TOOLS / "outbox"


def run_hook(script, args, stdin):
    return subprocess.run([sys.executable, "-I", str(OUTBOX / script), *map(str, args)], input=stdin,
                          capture_output=True, text=True, timeout=30)


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = self.d / "config.json"
        self.cfg.write_text(json.dumps({"recent_messages": 2, "max_chars_per_message": 5, "rule": "규칙 틀"}),
                            encoding="utf-8")

    def capture(self, prompt, sid="s-1"):
        p = run_hook("capture.py", [self.d], json.dumps({"session_id": sid, "prompt": prompt}))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "", "UserPromptSubmit 훅은 stdout에 아무것도 쓰면 안 된다(맥락 주입)")

    def test_capture_appends_with_seq(self):
        for m in ("첫째", "둘째"):
            self.capture(m)
        rows = [json.loads(x) for x in (self.d / "outbox" / "s-1.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["seq"], r["prompt"]) for r in rows], [(1, "첫째"), (2, "둘째")])
        self.assertTrue(all(r["ts"] for r in rows))

    def test_inject_recent_n_clipped_and_logged(self):
        for m in ("aaaaaaaa", "bb", "cccccccccc"):
            self.capture(m)
        p = run_hook("inject.py", [self.d, self.cfg], json.dumps({"session_id": "s-1", "source": "compact"}))
        self.assertEqual(p.returncode, 0)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        text = ctx["additionalContext"]
        self.assertTrue(text.startswith("규칙 틀"))
        self.assertNotIn("aaaa", text)               # N=2 → 최근 둘만
        self.assertIn("bb", text)
        self.assertIn("ccccc\n…(이하 5자 생략)", text)  # L=5
        log = json.loads((self.d / "inject-log.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((log["recent_messages"], log["max_chars_per_message"], log["seqs"], log["clipped_messages"]),
                         (2, 5, [2, 3], 1))
        self.assertEqual(log["chars_injected"], len(text))

    def test_inject_failure_logged_no_output_exit0(self):
        p = run_hook("inject.py", [self.d, self.cfg], json.dumps({"session_id": "없는-세션", "source": "compact"}))
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        err = json.loads((self.d / "hook-errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(err["hook"], "inject")
        self.assertFalse((self.d / "inject-log.jsonl").exists())

    def test_capture_bad_input_logged_exit0(self):
        p = run_hook("capture.py", [self.d], "not json")
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertIn("capture", (self.d / "hook-errors.jsonl").read_text(encoding="utf-8"))

    def test_capture_rejects_path_traversal_session(self):
        run_hook("capture.py", [self.d], json.dumps({"session_id": "../x", "prompt": "p"}))
        self.assertFalse((self.d / "x.jsonl").exists())
        self.assertTrue((self.d / "hook-errors.jsonl").exists())

    def test_hooks_do_not_read_task_files(self):
        for f in ("capture.py", "inject.py"):
            src = (OUTBOX / f).read_text(encoding="utf-8")
            self.assertNotIn("tasks", src)
            self.assertNotIn("steps", src)

    def test_compact_log_no_stdout(self):
        p = subprocess.run([sys.executable, "-I", str(TOOLS / "compact_log.py"), str(self.d)],
                           input=json.dumps({"hook_event_name": "PostCompact", "session_id": "s", "trigger": "auto",
                                             "compact_summary": "요약문"}), capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        row = json.loads((self.d / "compact-log.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((row["event"], row["trigger"], row["compact_summary"]), ("PostCompact", "auto", "요약문"))


    def test_real_config_injects_every_message_whole(self):
        """기본 설정(N·L)이 과제의 사용자 메시지 전체를 자르지 않고 다시 넣는다(이 조건이 재는 것)."""
        import run_task
        cfg = json.loads((OUTBOX / "config.json").read_text(encoding="utf-8"))
        msgs = run_task.build_messages(run_task.load_config())
        self.assertLessEqual(len(msgs), cfg["recent_messages"])
        self.assertLessEqual(max(len(m) for m in msgs), cfg["max_chars_per_message"])


FAKE_CLAUDE_TOKENS = """#!/usr/bin/env python3
import json, sys
text = sys.stdin.read()
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({"usage": {"input_tokens": len(text), "iterations": [{"input_tokens": len(text)}]},
                  "modelUsage": {model: {}}}))
"""


class CountInjectionTokensTest(unittest.TestCase):
    """주입문을 기록에서 다시 만들어(해시 확인) 가짜 claude로 잰다 — 유료 호출 없음."""

    def setUp(self):
        import count_injection_tokens as cit
        import score_run
        self.cit, self.sr = cit, score_run
        self.run = Path(tempfile.mkdtemp())
        data = self.run / "hookdata"
        cfg = json.loads((OUTBOX / "config.json").read_text(encoding="utf-8"))
        conf = self.run / "c.json"
        conf.write_text(json.dumps(cfg), encoding="utf-8")
        for m in ("첫 메시지 " * 50, "둘째", "셋째"):
            run_hook("capture.py", [data], json.dumps({"session_id": "s-1", "prompt": m}))
            run_hook("inject.py", [data, conf], json.dumps({"session_id": "s-1", "source": "compact"}))
        inj = [json.loads(x) for x in (data / "inject-log.jsonl").read_text(encoding="utf-8").splitlines()]
        (self.run / "run.json").write_text(json.dumps({
            "condition": "outbox", "model": "claude-fake-1", "session_id": "s-1", "outbox_config": cfg,
            "injections": inj}), encoding="utf-8")
        self.bin = Path(tempfile.mkdtemp()) / "claude"
        self.bin.write_text(FAKE_CLAUDE_TOKENS, encoding="utf-8")
        self.bin.chmod(0o755)

    def test_rebuilds_and_measures(self):
        texts = [t for _, t in self.cit.contexts(self.run)]
        self.assertEqual(len(texts), 3)
        self.assertIn("[사용자 메시지 3 ·", texts[2])
        self.assertIsNone(self.sr.injected_tokens(self.run, 3))  # 재기 전 = 미측정
        r = self.cit.measure(self.run, "claude-fake-1", 2, tempfile.mkdtemp(), str(self.bin))
        self.assertTrue(r["complete"])
        self.assertEqual([x["tokens"] for x in r["injections"]], [len(t) + 2 for t in texts])  # 꼬리 앞 줄바꿈 2자
        self.assertEqual(self.sr.injected_tokens(self.run, 3), sum(len(t) + 2 for t in texts))
        self.assertIsNone(self.sr.injected_tokens(self.run, 4))  # 주입 수가 다르면 미측정

    def test_hash_mismatch_stops_before_calls(self):
        p = self.run / "hookdata" / "outbox" / "s-1.jsonl"
        p.write_text(p.read_text(encoding="utf-8").replace("둘째", "바뀐 글"), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.cit.measure(self.run, "claude-fake-1", 1, tempfile.mkdtemp(), str(self.bin))
        self.assertFalse((self.run / "injection-tokens.json").exists())

    def test_workdir_inside_repo_refused(self):
        with self.assertRaises(SystemExit):
            self.cit.measure(self.run, "claude-fake-1", 1, str(TOOLS), str(self.bin))


if __name__ == "__main__":
    unittest.main()
