import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TOOLS, assistant, write

import containers
import run_task as rt

FAKE_DOCKER = TOOLS / "tests" / "fake_docker.py"
TOKEN = "sk-fake-oat01-TOKENVALUE123"


def fake_cfg(base):
    cfg = rt.load_config()
    skel = base / "skel"
    write(skel, "build.gradle.kts", "plugins {}\ndependencies {}\n")
    write(skel, "src/main/resources/db/migration/.gitkeep", "")
    os.chmod(FAKE_DOCKER, 0o755)
    tok = base / "claude.env"
    tok.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    os.chmod(tok, 0o600)
    cfg.update(run_root=str(base / "root"), runs_dir=str(base / "runs"), skeleton_dir=str(skel),
               docker_bin=str(FAKE_DOCKER), token_env_file=str(tok))
    return cfg


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(model="claude-fake-1", condition="compact", rep=1, last_step=2, turn_timeout=60,
                                  max_tokens=None, max_wall=None, max_budget_usd=None, keep_root=False,
                                  allow_concurrent=False), **kw)


def docker_leftovers(state):
    return {d: sorted(p.name for p in (state / d).iterdir()) for d in ("containers", "networks", "volumes")
            if (state / d).is_dir() and any((state / d).iterdir())}


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
        self.assertEqual({k: v for k, v in o.items() if k != "hooks"}, {k: v for k, v in c.items() if k != "hooks"})

    def test_auto_memory_off_in_every_condition(self):
        for cond in rt.CONDITIONS:
            self.assertIs(rt.settings_for(cond, self.cfg)["autoMemoryEnabled"], False, cond)
            self.assertEqual(rt.container_env(self.cfg, cond)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1", cond)
        envs = [{k: v for k, v in rt.container_env(self.cfg, c).items() if k != "CLAUDE_CODE_AUTO_COMPACT_WINDOW"}
                for c in rt.CONDITIONS]
        self.assertEqual(envs[0], envs[1])
        self.assertEqual(envs[0], envs[2])  # 조건 간 환경 차이는 컴팩트 기준값뿐

    def test_auto_memory_files(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(rt.auto_memory_files(d / "none"), [])
        write(d, "-work/sid.jsonl", "{}")
        (d / "-work" / "memory").mkdir()          # 빈 폴더는 파일이 아니다
        self.assertEqual(rt.auto_memory_files(d), [])
        write(d, "-work/memory/MEMORY.md", "x")
        write(d, "-work/memory/sub/rules.md", "x")
        self.assertEqual(rt.auto_memory_files(d), ["-work/memory/MEMORY.md", "-work/memory/sub/rules.md"])

    def test_container_paths_in_hooks_and_deny_rules(self):
        s = rt.settings_for("outbox", self.cfg)
        cmds = [h["command"] for groups in s["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertTrue(all(c.startswith("/usr/bin/python3 -I /cx/hooks/") for c in cmds), cmds)
        self.assertTrue(all(" /cx/hookdata" in c for c in cmds), cmds)
        p = s["permissions"]
        for t in ("Read", "Edit", "Write", "Grep", "Glob"):
            self.assertIn(f"{t}(//cx/hooks/**)", p["deny"])
            self.assertIn(f"{t}(//cx/hookdata/**)", p["deny"])
        self.assertIn("Edit(//work/.claude/**)", p["deny"])
        self.assertIn("WebFetch", p["deny"])
        self.assertIn("WebSearch", p["deny"])
        self.assertFalse([d for d in p["deny"] if str(Path.home()) in d], "호스트 경로 deny 규칙은 없어야 함")
        cmd = rt.claude_cmd(self.cfg, "m", "sid", True)
        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Bash,Read,Edit,Write,Grep,Glob")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/cx/hooks/empty-mcp.json")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project")
        self.assertIn("--resume", rt.claude_cmd(self.cfg, "m", "sid", False))

    def test_container_env(self):
        e = rt.container_env(self.cfg, "control")
        self.assertEqual(e["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "1000000")
        self.assertEqual(rt.container_env(self.cfg, "outbox")["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "100000")
        self.assertEqual((e["HOME"], e["CLAUDE_CONFIG_DIR"]), ("/home/agent", "/home/agent/.claude"))
        self.assertEqual(e["DB_URL"], "jdbc:postgresql://db:5432/booking")
        self.assertFalse([k for k in e if "TOKEN" in k or k.startswith("ANTHROPIC")])


class ContainerArgsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = rt.load_config()
        self.names = rt.resource_names(self.cfg, "fake-1-compact-r1-20260101T000000")

    def args(self, cond, name="cx-x-t00"):
        return rt.agent_create_args(self.cfg, self.names, "/tmp/cx-run", cond, name, ["claude", "-p"])

    def test_mounts_user_and_isolation(self):
        a = self.args("compact")
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "--mount"]
        self.assertEqual(mounts, ["type=bind,src=/tmp/cx-run/work,dst=/work",
                                  "type=bind,src=/tmp/cx-run/hooks,dst=/cx/hooks,readonly",
                                  f"type=volume,src={self.names['home']},dst=/home/agent",
                                  f"type=volume,src={self.names['hookdata']},dst=/cx/hookdata"])
        rw_binds = [m for m in mounts if m.startswith("type=bind") and not m.endswith(",readonly")]
        self.assertEqual(len(rw_binds), 1)
        joined = " ".join(a)
        self.assertNotIn("docker.sock", joined)
        for bad in ("--privileged", "-v", "--volume", "--volumes-from", "--pid", "--ipc", "--cap-add", "--device"):
            self.assertFalse([x for x in a if x == bad or x.startswith(bad + "=")], bad)
        self.assertNotEqual(a[a.index("--network") + 1], "host")
        self.assertFalse([m for m in mounts if str(Path.home()) in m], "호스트 HOME 아래 경로는 마운트하지 않음")
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(a[a.index("--network") + 1], self.names["net"])
        self.assertEqual(a[a.index("--env-file") + 1], self.cfg["token_env_file"])
        self.assertIn("--cap-drop=ALL", a)
        self.assertNotIn(TOKEN, joined)
        self.assertEqual(a[a.index(self.cfg["image"]) + 1:], ["claude", "-p"])

    def test_conditions_differ_only_by_window(self):
        a, b = self.args("control"), self.args("outbox")
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        self.assertEqual(len(a), len(b))
        self.assertEqual(diff, [("CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000")])

    def test_resource_names_have_prefix(self):
        for k, v in self.names.items():
            self.assertTrue(v.startswith("cx-"), (k, v))
        self.assertEqual(rt.turn_container(self.names, 3), self.names["tag"] + "-t03")

    def test_docker_guard(self):
        d = containers.Docker("docker", "cx-")
        self.assertEqual(d.guard("cx-a"), "cx-a")
        for bad in ("payment-claude-x", "alert-system-postgres-1", "cxa"):
            with self.assertRaises(ValueError):
                d.guard(bad)
        with self.assertRaises(ValueError):
            containers.Docker("docker", "payment-")
        with self.assertRaises(ValueError):
            containers.Docker("docker", "")


class RemoveLabelTest(unittest.TestCase):
    """지우기 전에 이 실험의 레이블을 확인한다(가짜 docker)."""

    def test_remove_refuses_resource_without_experiment_label(self):
        base = Path(tempfile.mkdtemp())
        os.chmod(FAKE_DOCKER, 0o755)
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(base / "docker")}):
            d = containers.Docker(str(FAKE_DOCKER), "cx-")
            d.run("volume", "create", "--label", "other=1", "cx-foreign")
            d.create_volume("cx-mine", "run-x")
            err = d.remove("volume", "cx-foreign")
            self.assertIn("지우지 않음", err)
            self.assertTrue(d.exists("volume", "cx-foreign"))
            self.assertIsNone(d.remove("volume", "cx-mine"))
            self.assertFalse(d.exists("volume", "cx-mine"))
            d.run("create", "--name", "cx-c1", "--label", "x=y", "img")
            self.assertIn("지우지 않음", d.remove("container", "cx-c1"))
            d.run("create", "--name", "cx-c2", "--label", rt.LABEL, "img")
            self.assertIsNone(d.remove("container", "cx-c2"))


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tok(self, text, mode=0o600):
        p = self.d / "t.env"
        p.write_text(text)
        os.chmod(p, mode)
        return p

    def test_read_token_validation(self):
        key = "CLAUDE_CODE_OAUTH_TOKEN"
        self.assertEqual(rt.read_token(self.tok(f"{key}=abc\n"), key), "abc")
        for text, mode in ((f"{key}=abc\n", 0o644), (f"{key}=abc\nOTHER=1\n", 0o600), ("OTHER=abc\n", 0o600),
                           (f"{key}=\n", 0o600), ("", 0o600)):
            with self.assertRaises(ValueError, msg=(text, oct(mode))):
                rt.read_token(self.tok(text, mode), key)
        with self.assertRaises(ValueError):
            rt.read_token(self.d / "none.env", key)
        st = rt.token_file_status(self.tok(f"{key}=abc\n"))
        self.assertEqual((st["exists"], st["mode"]), (True, "0o600"))

    def test_scrub_redacts_contents_and_names(self):
        write(self.d / "s", "a/b.txt", f"x{TOKEN}y")
        write(self.d / "s", f"c/{TOKEN}.log", "clean")
        write(self.d / "s", "d/ok.txt", "nothing")
        hits = rt.scrub(self.d / "s", TOKEN)
        self.assertEqual(hits, ["a/b.txt", "c/REDACTED-SECRET.log"])
        self.assertEqual((self.d / "s/a/b.txt").read_text(), "x<REDACTED-SECRET>y")
        self.assertEqual(rt.scrub(self.d / "s", TOKEN), [])


def ts(sec):
    return f"2026-11-18T00:00:{sec:02d}+00:00"


INJ = {"ts": ts(2), "source": "compact"}


class ValidityTest(unittest.TestCase):
    def test_invalid_reasons(self):
        b, log = [{"turn": 1}], [{"event": "PreCompact", "ts": ts(1)}, {"event": "PostCompact", "ts": ts(2)}]
        self.assertEqual(rt.invalid_reasons("control", b, [], False, [], log), ["control_has_compaction"])
        self.assertEqual(rt.invalid_reasons("compact", [], [], False, []), ["no_compaction"])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log), [])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, [], log),
                         ["outbox_injection_count_mismatch", "outbox_boundary_without_injection"])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, [INJ], log), [])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, [INJ, INJ], log), ["outbox_injection_count_mismatch"])

    def test_each_boundary_needs_its_own_injection(self):
        """주입 수가 경계 수와 같아도, 한 경계에 주입이 없고 다른 경계에 둘이면 무효."""
        b = [{"turn": 1}, {"turn": 2}]
        log = [{"event": "PreCompact", "ts": ts(10)}, {"event": "PostCompact", "ts": ts(11)},
               {"event": "PreCompact", "ts": ts(20)}, {"event": "PostCompact", "ts": ts(21)}]
        good = [{"ts": ts(12)}, {"ts": ts(22)}]
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, good, log), [])
        self.assertEqual(rt.boundary_injections(log, good, 2), [{"boundary": 0, "injection": 0},
                                                                {"boundary": 1, "injection": 1}])
        both_late = [{"ts": ts(22)}, {"ts": ts(23)}]  # 첫 경계 뒤 주입 없음, 둘째 경계 뒤 둘
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, both_late, log), ["outbox_boundary_without_injection"])
        self.assertEqual(rt.boundary_injections(log, both_late, 2)[0]["injection"], None)
        before = [{"ts": ts(5)}, {"ts": ts(22)}]  # 첫 경계보다 앞선 주입은 그 경계의 것이 아님
        self.assertIn("outbox_boundary_without_injection", rt.invalid_reasons("outbox", b, [], False, before, log))
        no_ts = [{}, {}]
        self.assertIn("outbox_boundary_without_injection", rt.invalid_reasons("outbox", b, [], False, no_ts, log))
        self.assertIn("settings_tampered", rt.invalid_reasons("compact", b, [], True, [], log))
        self.assertEqual(rt.invalid_reasons("compact", b, [{"hit": "tool:WebFetch"}], False, [], log),
                         ["audit:tool:WebFetch"])

    def test_invalid_reasons_added_checks(self):
        b, log = [{"turn": 1}], [{"event": "PreCompact"}, {"event": "PostCompact"}]
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log[:1]), ["compact_hook_count_mismatch"])
        self.assertEqual(rt.invalid_reasons("control", [], [], False, [], log), ["compact_hook_count_mismatch"])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log, transcript_unparsed=True),
                         ["transcript_unparsed"])
        self.assertEqual(rt.invalid_reasons("outbox", b, [], False, [{"ts": "2026-11-18T00:00:00+00:00"}],
                                            [{"event": "PreCompact", "ts": "2026-11-18T00:00:00+00:00"}, log[1]],
                                            [{"hook": "capture"}]), ["outbox_capture_error"])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log, [{"hook": "capture"}]), [])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log, memory_files=[{"turn": 0, "path": "p"}]),
                         ["auto_memory_files"])
        self.assertEqual(rt.invalid_reasons("compact", b, [], False, [], log, collection_reads=[{}]),
                         ["collection_data_read"])

    def test_audit_paths_and_web(self):
        cfg = rt.load_config()
        home = str(Path.home())
        entries = [assistant(tools=[("Bash", {"command": f"cat {home}/project/x/README.md"}),
                                    ("Read", {"file_path": "/work/src/a.kt"}),
                                    ("Bash", {"command": "cat /cx/hookdata/outbox/s.jsonl"}),
                                    ("Bash", {"command": "cat /cx/hooks/inject.py"}),
                                    ("Grep", {"pattern": "x", "path": "/opt/seat-reservation-lab"}),
                                    ("WebSearch", {"query": "q"}), ("Task", {})])]
        hits = {h["hit"] for h in rt.audit(entries, cfg)}
        # 훅 데이터(아웃박스)는 막지 않고 재독 공변량으로 센다(RereadTest) — 감사 대상 아님
        self.assertEqual(hits, {f"{home}/project", "/cx/hooks", "seat-reservation-lab", "tool:WebSearch"})
        self.assertEqual(rt.unexpected_tools(entries, cfg), ["Task"])


SID = "0b6f7c1e-1111-2222-3333-444455556666"


def kinds(tools, start=0, end=None):
    hits = rt.reread_hits([assistant(tools=tools)], start, end, SID)
    return {k: [h["match"] for h in v] for k, v in hits.items() if v}


class RereadTest(unittest.TestCase):
    def test_session_log_paths(self):
        for inp in ({"command": "ls ~/.claude/projects"}, {"file_path": f"/home/agent/.claude/projects/-work/{SID}.jsonl"},
                    {"command": "find / -name '*.jsonl' 2>/dev/null | head"}, {"command": "cat $CLAUDE_CONFIG_DIR/x"},
                    {"pattern": f"{SID}"}):
            self.assertIn("session_log", kinds([("Bash", inp)]), inp)

    def test_tool_results_read_is_not_reread(self):
        p = f"/home/agent/.claude/projects/-work/{SID}/tool-results/toolu_01.txt"
        self.assertEqual(kinds([("Read", {"file_path": p})]), {})

    def test_background_task_output_is_not_reread(self):
        """CLI 백그라운드 작업 출력(/tmp/claude-<uid>/<프로젝트>/<세션 id>/tasks/<id>.output)은 세션 id가 들어 있어도
        세션 기록 재독이 아니다. 같은 세션 id의 실제 세션 기록 읽기는 그대로 센다."""
        out = f"/tmp/claude-1000/-work/{SID}/tasks/bfr9l0nsk.output"
        self.assertEqual(kinds([("Bash", {"command": f"tail -30 {out}"})]), {})
        self.assertEqual(kinds([("Read", {"file_path": out})]), {})
        self.assertIn("session_log", kinds([("Bash", {"command": f"tail -30 {out}; cat ~/.claude/projects/-work/{SID}.jsonl"})]))
        self.assertIn("session_log", kinds([("Bash", {"command": f"grep -c user /home/agent/x/{SID}.jsonl"})]))
        self.assertIn("session_log", kinds([("Bash", {"command": f"ls /tmp/claude-1000/-work/{SID}/"})]))

    def test_outbox_and_root_search(self):
        k = kinds([("Bash", {"command": "cd /cx && cat hookdata/outbox/*"})])
        self.assertEqual(list(k), ["outbox_data"])
        self.assertIn("root_search", kinds([("Bash", {"command": "cd /work\ngrep -rn 'Clock' / 2>/dev/null"})]))
        self.assertIn("root_search", kinds([("Grep", {"pattern": "x", "path": "/"})]))
        self.assertIn("root_search", kinds([("Bash", {"command": "find /cx -type f"})]))
        self.assertEqual(kinds([("Bash", {"command": "find src/main -name '*.kt'"}),
                                ("Grep", {"pattern": "x", "path": "/work/src"})]), {})

    def test_memo_files(self):
        for inp in ({"file_path": "/work/CLAUDE.md", "content": "x"}, {"file_path": "/work/docs/decisions.md"},
                    {"command": "cat > .claude/notes.txt <<EOF\nx\nEOF"}, {"file_path": "/work/docs/작업-규칙.md"},
                    {"command": "echo x >> NOTES.md"}):
            self.assertEqual(list(kinds([("Write", inp)])), ["memo"], inp)
        for inp in ({"file_path": "/work/README.md"}, {"file_path": "/work/.claude/settings.json"},
                    {"file_path": "/work/src/main/kotlin/com/example/booking/seat/HoldRules.kt"}):
            self.assertEqual(kinds([("Read", inp)]), {}, inp)

    def test_range_and_collection_data(self):
        entries = [assistant(tools=[("Bash", {"command": "cat /cx/hookdata/compact-log.jsonl"})]),
                   assistant(tools=[("Bash", {"command": "ls"})])]
        self.assertEqual(rt.reread_hits(entries, 1, 2, SID)["outbox_data"], [])
        self.assertEqual(len(rt.reread_hits(entries, 0, 1, SID)["outbox_data"]), 1)
        self.assertEqual([h["match"] for h in rt.collection_data_reads(entries)], ["compact-log"])


class PrepareTest(unittest.TestCase):
    def test_prepare_root(self):
        base = Path(tempfile.mkdtemp())
        cfg = fake_cfg(base)
        head = rt.prepare_root(cfg, "outbox", rt.settings_text(rt.settings_for("outbox", cfg)))
        work = base / "root" / "work"
        log = subprocess.run(["git", "log", "--format=%s %an <%ae>"], cwd=work, capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "skeleton booking <booking@example.invalid>")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout
        self.assertEqual(status, "")  # .claude/ 는 info/exclude
        self.assertTrue((work / ".claude" / "settings.json").is_file())
        self.assertEqual(sorted(p.name for p in (base / "root" / "hooks").iterdir()),
                         ["capture.py", "compact_log.py", "empty-mcp.json", "inject.py", "outbox-config.json"])
        self.assertEqual(len(head), 40)
        with self.assertRaises(SystemExit):
            rt.prepare_root(cfg, "outbox", "{}")

    def test_skeleton_commit_sha_is_fixed(self):
        heads = []
        for _ in range(2):
            base = Path(tempfile.mkdtemp())
            heads.append(rt.prepare_root(fake_cfg(base), "compact", "{}"))
        self.assertEqual(heads[0], heads[1])


class RunLoopTest(unittest.TestCase):
    def run_fake(self, cond, expect_exit=None, **kw):
        base = Path(tempfile.mkdtemp())
        cfg = fake_cfg(base)
        env = {k: kw.pop(k) for k in list(kw) if k.startswith("FAKE_")}
        env["FAKE_DOCKER_STATE"] = str(base / "docker")
        with mock.patch.dict(os.environ, env):
            rc = rt.run(cfg, Args(condition=cond, **kw))
        rdir = next((base / "runs").iterdir())
        return rc, json.loads((rdir / "run.json").read_text(encoding="utf-8")), rdir, base

    def calls(self, base):
        return [json.loads(x) for x in (base / "docker" / "calls.jsonl").read_text().splitlines()]

    def test_outbox_run_end_to_end(self):
        rc, run, rdir, base = self.run_fake("outbox")
        self.assertEqual((rc, run["status"], run["invalid"], run["failure_reasons"]), (0, "complete", False, []))
        self.assertEqual(len(run["turns"]), 3)
        self.assertEqual([(b["turn"], b["position"]) for b in run["boundaries"]],
                         [(1, "before_prompt"), (2, "before_prompt")])
        self.assertEqual(len(run["injections"]), 2)
        self.assertEqual(run["outbox_boundary_injections"], [{"boundary": 0, "injection": 0}, {"boundary": 1, "injection": 1}])
        self.assertGreater(run["injections"][0]["chars_injected"], 0)
        self.assertEqual(run["compact_window_env"], "100000")
        self.assertEqual(run["image_id"], "sha256:fakeimage")
        self.assertEqual(run["cli_version"], "0.0.0 (fake)")
        for k in range(3):
            t = rdir / "turns" / f"{k:02d}"
            for f in ("result.json", "stderr.txt", "transcript.jsonl", "boundaries.json", "meta.json", "container.json"):
                self.assertTrue((t / f).is_file(), f"{t}/{f}")
        self.assertEqual(run["turns"][2]["container"]["exit_code"], 0)
        self.assertEqual(run["messages_sha256"], [t["message_sha256"] for t in run["turns"]])
        self.assertEqual(run["totals"]["tokens"], 300)
        self.assertEqual(run["secret_scan"]["hits"], {"work": [], "scratch": [], "record": [], "git_objects": []})
        self.assertTrue(run["secret_scan"]["archives_from_scanned"])
        self.assertTrue(run["measurement_complete"])
        self.assertEqual(run["totals"]["cost_usd"], 0.03)
        self.assertEqual(run["messages_sha256_all"], rt.messages_digest(run["messages_sha256"]))
        self.assertEqual([len(t["tracked_diff_sha256"]) for t in run["turns"]], [64] * 3)
        self.assertEqual([t["reread"] for t in run["turns"]], [{k: [] for k in rt.REREAD}] * 3)
        self.assertEqual(run["cleanup_errors"], [])
        self.assertEqual(docker_leftovers(base / "docker"), {})
        self.assertFalse((base / "root").exists())
        with tarfile.open(rdir / "work.tar.gz") as tf:
            self.assertIn("work/build.gradle.kts", tf.getnames())
        with tarfile.open(rdir / "claude-home.tar.gz") as tf:
            self.assertIn(f"claude-home/projects/-work/{run['session_id']}.jsonl", tf.getnames())
        outbox = (rdir / "hookdata" / "outbox" / f"{run['session_id']}.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(outbox.splitlines()), 3)
        creates = [c for c in self.calls(base) if c[0] == "create" and "-t0" in c[c.index("--name") + 1]]
        self.assertEqual(len(creates), 3)
        self.assertTrue(all("--env-file" in c for c in creates))
        self.assertFalse([c for c in self.calls(base) if TOKEN in json.dumps(c)], "토큰 값이 docker 인자에 나오면 안 됨")

    def test_control_is_valid_without_compaction(self):
        rc, run, *_ = self.run_fake("control")
        self.assertEqual(run["boundaries"], [])
        self.assertFalse(run["invalid"])
        self.assertEqual(run["compact_window_env"], "1000000")
        self.assertEqual(run["auto_memory"], {"settings": {"autoMemoryEnabled": False},
                                              "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}})
        self.assertEqual(run["auto_memory_files"], [])
        self.assertEqual([t["auto_memory_files"] for t in run["turns"]], [[]] * 3)

    def test_auto_memory_files_make_run_invalid(self):
        rc, run, rdir, _ = self.run_fake("control", FAKE_MEMORY_TURN="1")
        self.assertEqual(run["invalid_reasons"], ["auto_memory_files"])
        self.assertEqual(run["auto_memory_files"], [{"turn": 1, "path": "-work/memory/MEMORY.md"}])
        self.assertEqual([t["auto_memory_files"] for t in run["turns"]], [[]] + [["-work/memory/MEMORY.md"]] * 2)

    def test_turn_failure_aborts_keeps_record_and_cleans_up(self):
        rc, run, rdir, base = self.run_fake("compact", FAKE_FAIL_TURN="1")
        self.assertEqual((rc, run["status"]), (2, "aborted"))
        self.assertFalse(run["measurement_complete"])
        self.assertIn("turn 1", run["abort_reason"])
        self.assertEqual(run["turns"][1]["container"]["exit_code"], 1)
        self.assertEqual(len(run["turns"]), 2)
        self.assertTrue((rdir / "work.tar.gz").is_file())
        self.assertEqual(docker_leftovers(base / "docker"), {})

    def test_token_cap(self):
        rc, run, *_ = self.run_fake("compact", max_tokens=150)
        self.assertEqual(run["status"], "aborted")
        self.assertTrue(run["abort_reason"].startswith("token_cap at turn 1"))

    def test_forbidden_path_in_tool_input_invalid(self):
        rc, run, *_ = self.run_fake("compact", FAKE_TOOL_CMD="cat /opt/seat-reservation-lab/docs/adr.md")
        self.assertTrue(run["invalid"])
        self.assertIn("audit:seat-reservation-lab", run["invalid_reasons"])

    def test_token_leak_fails_run_and_is_redacted(self):
        rc, run, rdir, base = self.run_fake("compact", FAKE_LEAK_TURN="0")
        self.assertEqual((rc, run["status"]), (3, "failed"))
        self.assertIn("secret_leak", run["failure_reasons"])
        self.assertEqual(run["secret_scan"]["hits"]["work"], ["leak.txt"])
        self.assertTrue((rdir / "SECRET-LEAK.txt").is_file())
        with tarfile.open(rdir / "work.tar.gz") as tf:
            self.assertEqual(tf.extractfile("work/leak.txt").read(), b"token=<REDACTED-SECRET>\n")
        self.assertNotIn(TOKEN, (rdir / "run.json").read_text())

    def test_token_in_git_objects_fails_run_and_withholds_work_archive(self):
        rc, run, rdir, base = self.run_fake("compact", FAKE_COMMIT_LEAK_TURN="0")
        self.assertEqual((rc, run["status"]), (3, "failed"))
        self.assertIn("secret_leak", run["failure_reasons"])
        self.assertEqual((run["secret_scan"]["hits"]["work"], run["secret_scan"]["hits"]["git_objects"]), ([], [".git"]))
        self.assertEqual(run["secret_scan"]["work_archive_withheld"], "git_objects")
        self.assertFalse((rdir / "work.tar.gz").exists())
        self.assertNotIn("work_archive", run)
        self.assertTrue((rdir / "claude-home.tar.gz").is_file())

    def test_git_objects_hits(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(rt.git_objects_hits(d, TOKEN), [])  # 저장소 없음
        write(d, "a.txt", "clean")
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        g = ["git", "-c", "user.name=a", "-c", "user.email=a@e.invalid"]
        subprocess.run([*g, "add", "-A"], cwd=d, check=True)
        subprocess.run([*g, "commit", "-q", "-m", "x"], cwd=d, check=True)
        self.assertEqual(rt.git_objects_hits(d, TOKEN), [])
        write(d, "a.txt", TOKEN)
        subprocess.run([*g, "commit", "-qam", "y"], cwd=d, check=True)
        write(d, "a.txt", "clean")
        self.assertEqual(rt.git_objects_hits(d, TOKEN), [".git"])

    def test_scan_error_marks_archives_unscanned(self):
        """검사가 실패하면 압축본이 검사한 내용에서 만들어졌다고 기록하지 않는다(실행은 failed)."""
        orig = rt.scrub

        def boom(base, secret, skip_suffixes=()):
            if Path(base).name == "work":
                raise OSError("scan failed")
            return orig(base, secret, skip_suffixes)
        with mock.patch.object(rt, "scrub", boom):
            rc, run, rdir, base = self.run_fake("compact")
        self.assertEqual((run["status"], run["secret_scan"]["archives_from_scanned"]), ("failed", False))
        self.assertIn("secret_scan_error", run["failure_reasons"])

    def test_unparsed_transcript_line_invalidates(self):
        rc, run, *_ = self.run_fake("compact", FAKE_BAD_LINE_TURN="2")
        self.assertIn("transcript_unparsed", run["invalid_reasons"])
        self.assertEqual((run["transcript_unparsed_turns"], run["turns"][2]["transcript_bad_lines"]), ([2], 1))

    def test_collection_data_read_invalidates_and_outbox_read_is_covariate(self):
        rc, run, *_ = self.run_fake("compact", FAKE_TOOL_CMD="cat /cx/hookdata/compact-log.jsonl")
        self.assertIn("collection_data_read", run["invalid_reasons"])
        self.assertEqual([len(t["reread"]["outbox_data"]) for t in run["turns"]], [1, 1, 1])
        rc, run, *_ = self.run_fake("compact", FAKE_TOOL_CMD="cat /cx/hookdata/outbox/*.jsonl")
        self.assertFalse(run["invalid"], run["invalid_reasons"])
        self.assertEqual(len(run["turns"][1]["reread"]["outbox_data"]), 1)

    def test_keep_root_scans_scratch_session_copies(self):
        rc, run, rdir, base = self.run_fake("compact", keep_root=True, FAKE_TOOL_CMD=f"echo {TOKEN}")
        self.assertIn("secret_leak", run["failure_reasons"])
        self.assertTrue(run["measurement_complete"])  # 측정은 끝났다 — 운영 문제는 failure_reasons에 따로
        self.assertTrue([h for h in run["secret_scan"]["hits"]["scratch"] if h.startswith("projects/")])
        left = [p for p in (base / "root" / "scratch").rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
        self.assertEqual(left, [])

    def test_cleanup_failure_fails_run(self):
        rc, run, rdir, base = self.run_fake("compact", FAKE_DOCKER_FAIL_RM="-net")
        self.assertTrue(run["measurement_complete"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("docker_cleanup_failed", run["failure_reasons"])
        self.assertTrue(any("-net" in e for e in run["cleanup_errors"]))

    def test_refuses_while_other_experiment_runs(self):
        base = Path(tempfile.mkdtemp())
        cfg = fake_cfg(base)
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(base / "docker"),
                                          "FAKE_DOCKER_PS": "payment-claude-c01-a1"}):
            with self.assertRaises(SystemExit):
                rt.run(cfg, Args())
        self.assertFalse((base / "root").exists())
        self.assertFalse((base / "runs").exists())
        self.assertEqual(docker_leftovers(base / "docker"), {})
        made = [c for c in self.calls(base) if c[0] in ("create", "run") or c[:2] in (["network", "create"],
                                                                                      ["volume", "create"])]
        self.assertEqual(made, [])


if __name__ == "__main__":
    unittest.main()
