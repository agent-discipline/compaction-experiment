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


if __name__ == "__main__":
    unittest.main()
