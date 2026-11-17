import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TOOLS, assistant, write

import run_task as rt
import transcript

FAKE = TOOLS / "tests" / "fake_claude.py"


def fake_cfg(base):
    cfg = rt.load_config()
    skel = base / "skel"
    write(skel, "build.gradle.kts", "plugins {}\ndependencies {}\n")
    write(skel, "src/main/resources/db/migration/.gitkeep", "")
    os.chmod(FAKE, 0o755)
    creds = base / "host-creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 1_000_000}}))
    cfg.update(run_root=str(base / "root"), runs_dir=str(base / "runs"), skeleton_dir=str(skel),
               claude_bin=str(FAKE), credentials_file=str(creds))
    return cfg


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(model="claude-fake-1", condition="compact", rep=1, last_step=2, turn_timeout=60,
                                  max_tokens=None, max_wall=None, max_budget_usd=None, no_creds_writeback=False,
                                  keep_root=False), **kw)


class MessagesTest(unittest.TestCase):
    def test_first_message_is_constraints_then_s0(self):
        cfg = rt.load_config()
        msgs = rt.build_messages(cfg)
        s0 = (Path(cfg["task_dir"]) / "steps" / "S0.md").read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(len(msgs), 9)
        self.assertTrue(msgs[0].startswith("시작 전에"))
        self.assertTrue(msgs[0].endswith("\n\n" + s0))
        self.assertIn("8. 저와의 대화는 항상 한국어로", msgs[0])
        self.assertNotIn("```", msgs[0].split("\n\n" + s0)[0])
        self.assertEqual(rt.build_messages(cfg, 1), msgs[:2])

    def test_send_block_missing_raises(self):
        with self.assertRaises(ValueError):
            rt.extract_send_block("# 제목\n없음")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rt.load_config()

    def test_conditions_differ_only_by_outbox_hooks(self):
        c, k, o = (rt.settings_for(x, self.cfg) for x in ("control", "compact", "outbox"))
        self.assertEqual(c, k)
        self.assertEqual(o["permissions"], c["permissions"])
        self.assertEqual(set(o["hooks"]) - set(c["hooks"]), {"UserPromptSubmit", "SessionStart"})
        self.assertEqual({e: o["hooks"][e] for e in c["hooks"]}, c["hooks"])
        self.assertEqual(o["hooks"]["SessionStart"][0]["matcher"], "compact")

    def test_deny_rules_and_tools(self):
        p = rt.settings_for("compact", self.cfg)["permissions"]
        home = str(Path.home())
        for t in ("Read", "Edit", "Write", "Grep", "Glob"):
            self.assertIn(f"{t}(/{home}/project/**)", p["deny"])
        self.assertIn("WebFetch", p["deny"])
        self.assertIn("WebSearch", p["deny"])
        self.assertTrue(p["blockReadsOutsideWorkingDirectories"])
        cmd = rt.claude_cmd(self.cfg, "m", "sid", True)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Bash,Read,Edit,Write,Grep,Glob")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project")
        self.assertIn("--resume", rt.claude_cmd(self.cfg, "m", "sid", False))

    def test_env_window_and_dropped(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "SOMETHING": str(Path.home() / "project" / "a")}):
            env, dropped = rt.sandbox_env(self.cfg, "control")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "1000000")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIn("SOMETHING", dropped)
        self.assertEqual(rt.sandbox_env(self.cfg, "outbox")[0]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "100000")


class ValidityTest(unittest.TestCase):
    def test_invalid_reasons(self):
        b = [{"turn": 1}]
        self.assertEqual(rt.invalid_reasons("control", b, [], False, []), ["control_has_compaction"])
        self.assertEqual(rt.invalid_reasons("compact", [], [], False, []), ["no_compaction"])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, []), [])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, []), ["outbox_injection_missing"])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, [{}]), [])
        self.assertIn("settings_tampered", rt.invalid_reasons("compact", b, [], True, []))
        self.assertEqual(rt.invalid_reasons("compact", b, [{"hit": "tool:WebFetch"}], False, []),
                         ["audit:tool:WebFetch"])

    def test_audit_paths_and_web(self):
        cfg = rt.load_config()
        home = str(Path.home())
        entries = [assistant(tools=[("Bash", {"command": f"cat {home}/project/x/README.md"}),
                                    ("Read", {"file_path": "/tmp/cx-run/work/src/a.kt"}),
                                    ("Grep", {"pattern": "x", "path": "/opt/seat-reservation-lab"}),
                                    ("WebSearch", {"query": "q"}), ("Task", {})])]
        hits = {h["hit"] for h in rt.audit(entries, cfg)}
        self.assertEqual(hits, {f"{home}/project", "seat-reservation-lab", "tool:WebSearch"})
        self.assertEqual(rt.unexpected_tools(entries, cfg), ["Task"])


class CredsTest(unittest.TestCase):
    def test_copy_in_and_writeback_only_when_newer(self):
        d = Path(tempfile.mkdtemp())
        host, sb = d / "h.json", d / "s.json"
        host.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 2000}}))
        rt.creds_in(host, sb)
        self.assertEqual(rt.creds_expiry(sb), 2.0)
        self.assertFalse(rt.creds_writeback(host, sb, d / "lock"))
        sb.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 5000}}))
        self.assertTrue(rt.creds_writeback(host, sb, d / "lock"))
        self.assertEqual(rt.creds_expiry(host), 5.0)
        self.assertEqual(oct(host.stat().st_mode & 0o777), "0o600")
        with self.assertRaises(FileNotFoundError):
            rt.creds_in(d / "none.json", sb)


class PrepareTest(unittest.TestCase):
    def test_prepare_root(self):
        base = Path(tempfile.mkdtemp())
        cfg = fake_cfg(base)
        head = rt.prepare_root(cfg, "outbox", rt.settings_text(rt.settings_for("outbox", cfg)))
        work = base / "root" / "work"
        log = subprocess.run(["git", "log", "--format=%s"], cwd=work, capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "skeleton")
        self.assertNotIn("[S", log)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout
        self.assertEqual(status, "")  # .claude/ 는 info/exclude
        self.assertTrue((work / ".claude" / "settings.json").is_file())
        self.assertTrue((base / "root" / "hooks" / "inject.py").is_file())
        self.assertEqual(len(head), 40)
        with self.assertRaises(SystemExit):
            rt.prepare_root(cfg, "outbox", "{}")

    def test_skeleton_commit_sha_is_fixed(self):
        heads = []
        for _ in range(2):
            base = Path(tempfile.mkdtemp())
            cfg = fake_cfg(base)
            heads.append(rt.prepare_root(cfg, "compact", "{}"))
        self.assertEqual(heads[0], heads[1])


class RunLoopTest(unittest.TestCase):
    def run_fake(self, cond, **kw):
        base = Path(tempfile.mkdtemp())
        cfg = fake_cfg(base)
        env = {k: kw.pop(k) for k in list(kw) if k.startswith("FAKE_")}
        with mock.patch.dict(os.environ, env):
            rc = rt.run(cfg, Args(condition=cond, **kw))
        rdir = next((base / "runs").iterdir())
        return rc, json.loads((rdir / "run.json").read_text(encoding="utf-8")), rdir, base, cfg

    def test_outbox_run_end_to_end(self):
        rc, run, rdir, base, cfg = self.run_fake("outbox")
        self.assertEqual((rc, run["status"], run["invalid"]), (0, "complete", False))
        self.assertEqual(len(run["turns"]), 3)
        self.assertEqual([(b["turn"], b["position"]) for b in run["boundaries"]],
                         [(1, "before_prompt"), (2, "before_prompt")])
        self.assertEqual(len(run["injections"]), 2)
        self.assertGreater(run["injections"][0]["chars_injected"], 0)
        self.assertEqual(run["compact_window_env"], "100000")
        for k in range(3):
            t = rdir / "turns" / f"{k:02d}"
            for f in ("result.json", "stderr.txt", "transcript.jsonl", "boundaries.json", "meta.json"):
                self.assertTrue((t / f).is_file(), f"{t}/{f}")
        self.assertEqual(run["messages_sha256"], [t["message_sha256"] for t in run["turns"]])
        self.assertEqual(run["totals"]["tokens"], 300)
        self.assertEqual(run["creds_written_back"], 1)
        self.assertEqual(rt.creds_expiry(cfg["credentials_file"]), 1001.0)
        self.assertFalse((base / "root").exists())
        with tarfile.open(rdir / "work.tar.gz") as tf:
            self.assertIn("work/build.gradle.kts", tf.getnames())
        outbox = (rdir / "hookdata" / "outbox" / f"{run['session_id']}.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(outbox.splitlines()), 3)

    def test_control_is_invalid_if_compaction_seen_and_valid_otherwise(self):
        rc, run, *_ = self.run_fake("control")
        self.assertEqual(run["boundaries"], [])
        self.assertFalse(run["invalid"])
        self.assertEqual(run["compact_window_env"], "1000000")

    def test_turn_failure_aborts_and_keeps_record(self):
        rc, run, rdir, *_ = self.run_fake("compact", FAKE_FAIL_TURN="1")
        self.assertEqual((rc, run["status"]), (2, "aborted"))
        self.assertIn("turn 1", run["abort_reason"])
        self.assertEqual(len(run["turns"]), 2)
        self.assertTrue((rdir / "work.tar.gz").is_file())

    def test_token_cap(self):
        rc, run, *_ = self.run_fake("compact", max_tokens=150)
        self.assertEqual(run["status"], "aborted")
        self.assertTrue(run["abort_reason"].startswith("token_cap at turn 1"))

    def test_forbidden_path_in_tool_input_invalid(self):
        # 호스트 프로젝트 경로를 값으로 가진 환경 변수는 샌드박스에 넘어가지 않으므로 이름 패턴으로 확인
        rc, run, *_ = self.run_fake("compact", FAKE_TOOL_CMD="cat /opt/seat-reservation-lab/docs/adr.md")
        self.assertTrue(run["invalid"])
        self.assertIn("audit:seat-reservation-lab", run["invalid_reasons"])


if __name__ == "__main__":
    unittest.main()
