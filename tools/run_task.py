#!/usr/bin/env python3
"""실행기 — 실행 1회 = (모델, 조건, 반복 번호). 단계 메시지를 한 턴씩 `claude -p`로 재생하고 턴마다 기록한다.

사용:
  python3 tools/run_task.py --model claude-sonnet-5-5 --condition compact --rep 1 \
      [--last-step 8] [--turn-timeout 5400] [--max-tokens N] [--max-wall 36000] [--max-budget-usd X] \
      [--keep-root] [--dry-run] [--check-env [--check-gradle]] [--config tools/experiment.json]

출력: <runs_dir>/<run id>/ (저장소 안, gitignore) — run.json(턴마다 갱신), settings.json, messages.json,
turns/NN/{result.json, stderr.txt, transcript.jsonl, boundaries.json, meta.json}, hookdata/, work.tar.gz.
작업 폴더는 <run_root>/work(기본 /tmp/cx-run/work — 호스트 프로젝트 폴더 밖, 실행마다 같은 경로).
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = ("control", "compact", "outbox")
COPY_IGNORE = shutil.ignore_patterns(".git", "build", ".gradle", ".kotlin", ".idea", "out")
SAFE_ENV_SHOWN = ("HOME", "TMPDIR", "JAVA_HOME", "GRADLE_USER_HOME", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                  "DISABLE_AUTOUPDATER")


# ---------- 설정 ----------

def load_config(path=None):
    p = Path(path) if path else ROOT / "tools" / "experiment.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["_path"] = str(p)
    for k in ("task_dir", "skeleton_dir", "runs_dir"):
        cfg[k] = str(resolve(cfg[k]))
    for k in ("run_root", "claude_bin", "credentials_file", "java_home", "gradle_user_home"):
        cfg[k] = str(Path(os.path.expanduser(cfg[k])))
    return cfg


def resolve(p):
    p = Path(os.path.expanduser(p))
    return p if p.is_absolute() else ROOT / p


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------- 메시지 ----------

def extract_send_block(constraints_md):
    """constraints.md의 '## 보낼 문장' 절 첫 ```text 블록 본문."""
    m = re.search(r"^## 보낼 문장.*?^```text\n(.*?)^```", constraints_md, re.S | re.M)
    if not m:
        raise ValueError("constraints.md에서 '## 보낼 문장'의 ```text 블록을 찾지 못함")
    return m.group(1).rstrip("\n")


def build_messages(cfg, last_step=None):
    """턴별 사용자 메시지(문자열). 첫 메시지 = 제약 블록 + 빈 줄 + S0."""
    task = Path(cfg["task_dir"])
    steps = cfg["steps"] if last_step is None else cfg["steps"][:last_step + 1]
    msgs = [(task / "steps" / f"{s}.md").read_text(encoding="utf-8").rstrip("\n") for s in steps]
    msgs[0] = extract_send_block((task / "constraints.md").read_text(encoding="utf-8")) + "\n\n" + msgs[0]
    return msgs


# ---------- 조건별 설정 ----------

def hook_cmd(py, run_root, script, *args):
    return " ".join([py, "-I", f"{run_root}/hooks/{script}", *args])


def settings_for(condition, cfg):
    """작업 폴더 .claude/settings.json 내용(dict). 조건 간 차이는 outbox 훅 유무뿐."""
    if condition not in CONDITIONS:
        raise ValueError(f"알 수 없는 조건: {condition}")
    r, home, py = cfg["run_root"], str(Path.home()), cfg["hook_python"]
    data = f"{r}/hookdata"
    collect = [{"hooks": [{"type": "command", "command": hook_cmd(py, r, "compact_log.py", data), "timeout": 30}]}]
    hooks = {"PreCompact": collect, "PostCompact": collect}
    if condition == "outbox":
        hooks["UserPromptSubmit"] = [{"hooks": [{"type": "command", "timeout": 30,
                                                 "command": hook_cmd(py, r, "capture.py", data)}]}]
        hooks["SessionStart"] = [{"matcher": "compact", "hooks": [{
            "type": "command", "timeout": 30,
            "command": hook_cmd(py, r, "inject.py", data, f"{r}/hooks/outbox-config.json")}]}]
    file_tools = ("Read", "Edit", "Write", "Grep", "Glob")
    deny = [f"{t}(/{home}/project/**)" for t in file_tools]
    # home/ 은 막지 않는다 — CLI가 큰 도구 출력을 home/.claude/projects/.../tool-results에 두고 에이전트가 Read로 읽는다
    deny += [f"{t}(/{r}/{d}/**)" for d in ("hooks", "hookdata") for t in file_tools]
    deny += [f"{t}(/{r}/work/.claude/**)" for t in ("Edit", "Write")]
    deny += list(cfg["forbidden_tools"])
    return {"permissions": {"allow": list(cfg["allowed_tools"]), "deny": deny,
                            "blockReadsOutsideWorkingDirectories": True,
                            "blockWritesOutsideWorkingDirectories": True},
            "hooks": hooks}


def settings_text(d):
    return json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def claude_cmd(cfg, model, session_id, first, budget=None):
    cmd = [cfg["claude_bin"], "-p", "--model", model, "--output-format", "json",
           "--setting-sources", "project", "--strict-mcp-config",
           "--mcp-config", f"{cfg['run_root']}/empty-mcp.json",
           "--tools", ",".join(cfg["allowed_tools"]),
           "--permission-mode", cfg["permission_mode"]]
    cmd += ["--session-id", session_id] if first else ["--resume", session_id]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    return cmd


def sandbox_env(cfg, condition):
    """호스트 환경에서 Claude 관련 변수와 프로젝트 폴더를 가리키는 변수를 빼고 임시 HOME 등을 넣는다.
    (env, 뺀 변수 이름 목록)"""
    proj = str(Path.home() / "project")
    dropped = sorted(k for k, v in os.environ.items()
                     if k.startswith(("CLAUDE", "ANTHROPIC")) or k in ("OLDPWD", "PWD") or proj in v)
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    r = cfg["run_root"]
    env.update({"HOME": f"{r}/home", "TMPDIR": f"{r}/tmp", "DISABLE_AUTOUPDATER": "1",
                "JAVA_HOME": cfg["java_home"], "GRADLE_USER_HOME": cfg["gradle_user_home"],
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": cfg["compact_window"][condition]})
    env["PATH"] = f"{cfg['java_home']}/bin:" + env.get("PATH", "")
    return env, dropped


# ---------- 자격 증명(내용은 읽어 출력·기록하지 않는다 — 만료 시각만 비교) ----------

def creds_expiry(p):
    try:
        return json.loads(Path(p).read_text())["claudeAiOauth"]["expiresAt"] / 1000
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def creds_in(host, sb):
    """샌드박스 사본이 없거나 호스트 쪽이 더 새것이면 복사."""
    if not Path(host).is_file():
        raise FileNotFoundError(f"자격 증명 파일 없음: {host}")
    if not Path(sb).is_file() or creds_expiry(host) > creds_expiry(sb):
        shutil.copy2(host, sb)
        os.chmod(sb, 0o600)


def creds_writeback(host, sb, lock_path):
    """샌드박스 안에서 토큰이 갱신(회전)됐으면 호스트 파일에 원자적으로 되쓴다(선례 방식)."""
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if creds_expiry(sb) > creds_expiry(host):
            tmp = Path(str(host) + ".tmp-cx")
            shutil.copy2(sb, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, host)
            return True
    return False


# ---------- 준비 ----------

def git(args, cwd, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


def prepare_root(cfg, condition, settings):
    r = Path(cfg["run_root"])
    if r.exists():
        raise SystemExit(f"{r} 가 이미 있다 — 이전 실행의 잔여물. 내용을 확인한 뒤 직접 지우고 다시 실행.")
    for d in ("home/.claude", "tmp", "hooks", "hookdata", "work"):
        (r / d).mkdir(parents=True)
    os.chmod(r, 0o700)
    (r / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    tools = ROOT / "tools"
    shutil.copy2(tools / "compact_log.py", r / "hooks" / "compact_log.py")
    if condition == "outbox":
        shutil.copy2(tools / "outbox" / "capture.py", r / "hooks" / "capture.py")
        shutil.copy2(tools / "outbox" / "inject.py", r / "hooks" / "inject.py")
        shutil.copy2(tools / "outbox" / "config.json", r / "hooks" / "outbox-config.json")
    sc = cfg["skeleton_commit"]
    (r / "home" / ".gitconfig").write_text(
        f"[user]\n\tname = {sc['name']}\n\temail = {sc['email']}\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    work = r / "work"
    skel = Path(cfg["skeleton_dir"])
    if not (skel / "build.gradle.kts").is_file():
        raise SystemExit(f"골격이 없다: {skel}")
    shutil.copytree(skel, work, dirs_exist_ok=True, ignore=COPY_IGNORE)
    genv = dict(os.environ, HOME=str(r / "home"), GIT_AUTHOR_DATE=sc["date"], GIT_COMMITTER_DATE=sc["date"])
    git(["init", "-q", "-b", "main"], work, genv)
    git(["add", "-A"], work, genv)
    git(["commit", "-q", "-m", sc["message"]], work, genv)
    with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.write(".claude/\n")
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(settings, encoding="utf-8")
    return git(["rev-parse", "HEAD"], work).stdout.strip()


# ---------- 감사·무효 ----------

def audit_patterns(cfg):
    return [p.format(real_home=str(Path.home()), run_root=cfg["run_root"]) for p in cfg["audit_forbidden_patterns"]]


def audit(entries, cfg):
    """도구 입력에서 금지 경로·금지 도구. [{line, tool, hit}]"""
    pats, bad_tools, out = audit_patterns(cfg), set(cfg["forbidden_tools"]), []
    for line, name, inp in transcript.tool_uses(entries):
        if name in bad_tools:
            out.append({"line": line, "tool": name, "hit": f"tool:{name}"})
        text = json.dumps(inp, ensure_ascii=False)
        out += [{"line": line, "tool": name, "hit": p} for p in pats if p in text]
    return out


def unexpected_tools(entries, cfg):
    allowed = set(cfg["allowed_tools"]) | set(cfg["forbidden_tools"])
    return sorted({name for _, name, _ in transcript.tool_uses(entries) if name not in allowed})


def invalid_reasons(condition, bounds, audit_hits, settings_tampered, injections):
    out = []
    if condition == "control" and bounds:
        out.append("control_has_compaction")
    if condition != "control" and not bounds:
        out.append("no_compaction")
    out += sorted({f"audit:{h['hit']}" for h in audit_hits})
    if settings_tampered:
        out.append("settings_tampered")
    if condition == "outbox" and len(injections) < len(bounds):
        out.append("outbox_injection_missing")
    return out


# ---------- 턴 ----------

def tokens_of(res):
    mu = res.get("modelUsage") or {}
    if mu:
        return sum((v.get("inputTokens") or 0) + (v.get("outputTokens") or 0) + (v.get("cacheReadInputTokens") or 0)
                   + (v.get("cacheCreationInputTokens") or 0) for v in mu.values())
    u = res.get("usage") or {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return sum(u.get(k) or 0 for k in keys) if u else None


def context_tokens_last(res):
    it = (res.get("usage") or {}).get("iterations") or []
    if not it:
        return None
    last = it[-1]
    return sum(last.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def run_turn(cmd, msg, cwd, env, timeout, out_path, err_path):
    """(exit_code, timed_out, wall_s)"""
    t0 = time.time()
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=fo, stderr=fe,
                             start_new_session=True)
        try:
            p.communicate(msg.encode("utf-8"), timeout=timeout)
            return p.returncode, False, time.time() - t0
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return p.returncode, True, time.time() - t0


def find_transcript(home, session_id):
    hits = sorted(Path(home, ".claude", "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def read_jsonl(p):
    p = Path(p)
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_json(p, obj):
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def archive_work(work, dest):
    def flt(ti):
        parts = Path(ti.name).parts
        return None if any(x in ("build", ".gradle", ".kotlin") for x in parts) else ti
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(work, arcname="work", filter=flt)


# ---------- 본체 ----------

def model_short(model):
    return re.sub(r"[^A-Za-z0-9.-]", "", model.replace("claude-", ""))


def make_run_id(model, condition, rep):
    return f"{model_short(model)}-{condition}-r{rep}-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"


def dry_run(cfg, a):
    settings = settings_text(settings_for(a.condition, cfg))
    msgs = build_messages(cfg, a.last_step)
    env, dropped = sandbox_env(cfg, a.condition)
    sid = "<session-uuid>"
    print(f"run_id(예): {make_run_id(a.model, a.condition, a.rep)}")
    print(f"run_root: {cfg['run_root']}  work: {cfg['run_root']}/work  기록: {cfg['runs_dir']}/<run id>/")
    print(f"골격: {cfg['skeleton_dir']} ({'있음' if Path(cfg['skeleton_dir'], 'build.gradle.kts').is_file() else '없음'})")
    print(f"settings.json sha256={sha256_bytes(settings.encode())}")
    print(settings, end="")
    print("env(보이는 값):", {k: env[k] for k in SAFE_ENV_SHOWN})
    print("env(뺀 변수 이름):", dropped)
    print("첫 턴 명령:", " ".join(claude_cmd(cfg, a.model, sid, True, a.max_budget_usd)), "< 메시지 0")
    print("이후 턴 명령:", " ".join(claude_cmd(cfg, a.model, sid, False, a.max_budget_usd)), "< 메시지 k")
    allm = hashlib.sha256()
    for k, m in enumerate(msgs):
        b = m.encode("utf-8")
        allm.update(sha256_bytes(b).encode())
        print(f"메시지 {k} ({cfg['steps'][k]}): {len(m)}자 sha256={sha256_bytes(b)}")
    print(f"메시지 전체 sha256={allm.hexdigest()}")
    if a.condition == "outbox":
        oc = (ROOT / "tools" / "outbox" / "config.json").read_bytes()
        print(f"outbox config sha256={sha256_bytes(oc)}: {oc.decode().strip()}")
    return 0


def check_env(cfg, with_gradle):
    """유료 호출 없이 임시 HOME에서 java·docker·git·claude(버전만)·(선택) gradle 테스트를 확인."""
    base = Path(tempfile.mkdtemp(prefix="cx-envcheck-"))
    try:
        c2 = dict(cfg, run_root=str(base))
        for d in ("home", "tmp"):
            (base / d).mkdir()
        env, _ = sandbox_env(c2, "compact")
        checks = [["java", "-version"], ["docker", "info", "--format", "{{.ServerVersion}}"],
                  ["git", "--version"], [cfg["claude_bin"], "--version"]]
        ok = True
        for c in checks:
            p = subprocess.run(c, env=env, cwd=base, capture_output=True, text=True, timeout=120)
            print(f"[{'ok' if p.returncode == 0 else 'FAIL'}] {' '.join(c)}: {(p.stdout + p.stderr).strip()[:300]}")
            ok &= p.returncode == 0
        if with_gradle:
            w = base / "work"
            shutil.copytree(cfg["skeleton_dir"], w, ignore=COPY_IGNORE)
            p = subprocess.run(["./gradlew", "test", "--no-daemon", "--console=plain"], env=env, cwd=w,
                               capture_output=True, text=True, timeout=3600)
            print(f"[{'ok' if p.returncode == 0 else 'FAIL'}] gradlew test (골격 사본, 임시 HOME): "
                  f"{p.stdout[-1500:]}{p.stderr[-800:]}")
            ok &= p.returncode == 0
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--condition", choices=CONDITIONS, default="compact")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--last-step", type=int, default=None, help="이 단계 번호까지만 보냄(스모크용, 기본 전체)")
    ap.add_argument("--turn-timeout", type=int, default=5400, help="턴 하나의 시간 상한(초)")
    ap.add_argument("--max-tokens", type=int, default=None, help="실행 전체 토큰 합 상한(modelUsage 합)")
    ap.add_argument("--max-wall", type=int, default=None, help="실행 전체 시간 상한(초)")
    ap.add_argument("--max-budget-usd", default=None, help="턴마다 claude --max-budget-usd 로 넘김")
    ap.add_argument("--no-creds-writeback", action="store_true", help="갱신된 토큰을 호스트에 되쓰지 않음")
    ap.add_argument("--keep-root", action="store_true", help="끝난 뒤 run_root를 지우지 않음")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-env", action="store_true")
    ap.add_argument("--check-gradle", action="store_true")
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.check_env:
        return check_env(cfg, a.check_gradle)
    if a.dry_run:
        return dry_run(cfg, a)
    return run(cfg, a)


def run(cfg, a):
    msgs = build_messages(cfg, a.last_step)
    settings = settings_text(settings_for(a.condition, cfg))
    run_id = make_run_id(a.model, a.condition, a.rep)
    rdir = Path(cfg["runs_dir"]) / run_id
    root = Path(cfg["run_root"])
    work, home = root / "work", root / "home"
    if root.exists():
        raise SystemExit(f"{root} 가 이미 있다 — 이전 실행의 잔여물. 내용을 확인한 뒤 직접 지우고 다시 실행.")
    try:
        skeleton_head = prepare_root(cfg, a.condition, settings)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)  # 이번 호출이 만든 것만 지운다(위에서 없음을 확인)
        raise
    (rdir / "turns").mkdir(parents=True)
    settings_sha = sha256_bytes(settings.encode())
    (rdir / "settings.json").write_text(settings, encoding="utf-8")
    write_json(rdir / "messages.json", [{"step": cfg["steps"][k], "sha256": sha256_bytes(m.encode()), "chars": len(m),
                                         "text": m} for k, m in enumerate(msgs)])
    env, dropped = sandbox_env(cfg, a.condition)
    cli = subprocess.run([cfg["claude_bin"], "--version"], capture_output=True, text=True).stdout.strip()
    session_id = str(uuid.uuid4())
    state = {
        "run_id": run_id, "model": a.model, "condition": a.condition, "rep": a.rep, "status": "running",
        "abort_reason": None, "invalid": None, "invalid_reasons": [], "warnings": [],
        "started_at": now_iso(), "ended_at": None, "cli_version": cli, "session_id": session_id,
        "run_root": str(root), "work_dir": str(work), "skeleton_commit": skeleton_head,
        "steps_planned": cfg["steps"][:len(msgs)], "compact_window_env": env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
        "settings_sha256": settings_sha, "env_dropped": dropped,
        "outbox_config": None, "outbox_config_sha256": None,
        "messages_sha256": [sha256_bytes(m.encode()) for m in msgs],
        "limits": {"turn_timeout": a.turn_timeout, "max_tokens": a.max_tokens, "max_wall": a.max_wall,
                   "max_budget_usd": a.max_budget_usd},
        "turns": [], "boundaries": [], "totals": {"tokens": None, "cost_usd": None, "wall_s": 0.0},
        "audit": [], "unexpected_tools": [], "injections": [], "compact_log": [], "hook_errors": [],
        "creds_written_back": 0,
    }
    if a.condition == "outbox":
        oc = (root / "hooks" / "outbox-config.json").read_bytes()
        state["outbox_config"] = json.loads(oc)
        state["outbox_config_sha256"] = sha256_bytes(oc)
    write_json(rdir / "run.json", state)
    sb_creds = home / ".claude" / ".credentials.json"
    line_ends, entries, prev_size, prev_hash = [], [], 0, None
    t_start, tokens_total, cost_total, tokens_known = time.time(), 0, 0.0, True
    tampered = False
    try:
        for k, msg in enumerate(msgs):
            tdir = rdir / "turns" / f"{k:02d}"
            tdir.mkdir()
            creds_in(cfg["credentials_file"], sb_creds)
            timeout, wall_limited = a.turn_timeout, False
            if a.max_wall:
                left = a.max_wall - (time.time() - t_start)
                if left <= 0:
                    state["status"], state["abort_reason"] = "aborted", f"time_cap before turn {k}"
                    break
                if left < timeout:
                    timeout, wall_limited = int(left), True
            cmd = claude_cmd(cfg, a.model, session_id, k == 0, a.max_budget_usd)
            started = time.time()
            rc, timed_out, wall = run_turn(cmd, msg, work, env, timeout, tdir / "result.json", tdir / "stderr.txt")
            ended = time.time()
            if not a.no_creds_writeback and creds_writeback(cfg["credentials_file"], sb_creds, root / ".creds.lock"):
                state["creds_written_back"] += 1
            try:
                res = json.loads((tdir / "result.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                res = None
            tpath = find_transcript(home, session_id)
            n_lines = None
            if tpath:
                raw = tpath.read_bytes()
                if prev_hash and sha256_bytes(raw[:prev_size]) != prev_hash:
                    state["warnings"].append(f"turn {k}: 세션 기록이 덧붙이기만 된 것이 아님(앞부분 변경)")
                prev_size, prev_hash = len(raw), sha256_bytes(raw)
                (tdir / "transcript.jsonl").write_bytes(raw)
                entries, bad = transcript.load(tdir / "transcript.jsonl")
                n_lines = len(entries)
                if bad:
                    state["warnings"].append(f"turn {k}: 세션 기록 파싱 실패 줄 {bad}")
            else:
                state["warnings"].append(f"turn {k}: 세션 기록 파일을 찾지 못함")
            line_ends.append(n_lines if n_lines is not None else (line_ends[-1] if line_ends else 0))
            tok = tokens_of(res) if res else None
            if tok is None:
                tokens_known = False
            else:
                tokens_total += tok
            cost = res.get("total_cost_usd") if res else None
            if cost is not None:
                cost_total += cost
            head = git(["rev-parse", "HEAD"], work, check=False).stdout.strip() or None
            cur_settings = (work / ".claude" / "settings.json")
            if not cur_settings.is_file() or sha256_bytes(cur_settings.read_bytes()) != settings_sha:
                tampered = True
            extra = sorted(p.name for p in (work / ".claude").iterdir()) if (work / ".claude").is_dir() else []
            turn = {"step": cfg["steps"][k], "index": k, "started_at": started, "ended_at": ended,
                    "started_iso": datetime.datetime.fromtimestamp(started, datetime.timezone.utc).isoformat(),
                    "wall_s": round(wall, 1), "exit_code": rc, "timed_out": timed_out,
                    "result_parsed": res is not None,
                    "is_error": res.get("is_error") if res else None, "subtype": res.get("subtype") if res else None,
                    "result_session_id": res.get("session_id") if res else None,
                    "num_turns": res.get("num_turns") if res else None,
                    "tokens": tok, "cost_usd": cost, "context_tokens_last": context_tokens_last(res) if res else None,
                    "usage": res.get("usage") if res else None, "model_usage": res.get("modelUsage") if res else None,
                    "git_head": head, "transcript_lines": n_lines, "claude_dir_files": extra,
                    "message_sha256": sha256_bytes(msg.encode())}
            if res and res.get("session_id") not in (None, session_id):
                state["warnings"].append(f"turn {k}: 결과의 session_id가 다름 ({res.get('session_id')})")
            state["turns"].append(turn)
            write_json(tdir / "meta.json", turn)
            state["boundaries"] = transcript.locate_boundaries(entries, line_ends, msgs)
            write_json(tdir / "boundaries.json", state["boundaries"])
            if (root / "hookdata").is_dir():
                shutil.copytree(root / "hookdata", rdir / "hookdata", dirs_exist_ok=True)
            state["totals"] = {"tokens": tokens_total if tokens_known else None, "cost_usd": round(cost_total, 4),
                               "wall_s": round(time.time() - t_start, 1)}
            failed = None
            if timed_out:
                failed = "time_cap" if wall_limited else "turn_timeout"
            elif rc != 0 or res is None or res.get("is_error") or res.get("subtype") not in (None, "success"):
                failed = f"turn_failed(exit={rc}, subtype={res.get('subtype') if res else None})"
            elif a.max_tokens and tokens_known and tokens_total > a.max_tokens:
                failed = "token_cap"
            elif a.max_wall and time.time() - t_start > a.max_wall:
                failed = "time_cap"
            if failed:
                state["status"], state["abort_reason"] = "aborted", f"{failed} at turn {k} ({cfg['steps'][k]})"
            write_json(rdir / "run.json", state)
            if failed:
                break
        else:
            state["status"] = "complete"
    except BaseException as e:  # 실행기 자체 오류·중단도 기록하고 다시 올린다
        state["status"], state["abort_reason"] = "aborted", f"runner_error: {e!r}"
        write_json(rdir / "run.json", state)
        raise
    finally:
        finalize(cfg, a, state, rdir, root, work, entries, tampered)
    return 0 if state["status"] == "complete" else 2


def finalize(cfg, a, state, rdir, root, work, entries, tampered):
    hd = rdir / "hookdata"
    if (root / "hookdata").is_dir():
        shutil.copytree(root / "hookdata", hd, dirs_exist_ok=True)
    state["injections"] = read_jsonl(hd / "inject-log.jsonl")
    state["compact_log"] = [{k: v for k, v in r.items() if k != "compact_summary"} | {
        "summary_chars": len(r.get("compact_summary") or "")} for r in read_jsonl(hd / "compact-log.jsonl")]
    state["hook_errors"] = read_jsonl(hd / "hook-errors.jsonl")
    state["audit"] = audit(entries, cfg)
    state["unexpected_tools"] = unexpected_tools(entries, cfg)
    reasons = invalid_reasons(state["condition"], state["boundaries"], state["audit"], tampered, state["injections"])
    state["invalid_reasons"], state["invalid"] = reasons, bool(reasons)
    state["ended_at"] = now_iso()
    try:
        if (work / "gradlew").is_file():
            env, _ = sandbox_env(cfg, state["condition"])
            subprocess.run(["./gradlew", "--stop"], cwd=work, env=env, capture_output=True, timeout=120)
        if work.is_dir():
            archive_work(work, rdir / "work.tar.gz")
            state["work_archive"] = "work.tar.gz"
    except Exception as e:
        state["warnings"].append(f"finalize: {e!r}")
    write_json(rdir / "run.json", state)
    if not a.keep_root:
        shutil.rmtree(root, ignore_errors=True)
    print(json.dumps({k: state[k] for k in ("run_id", "status", "abort_reason", "invalid", "invalid_reasons")},
                     ensure_ascii=False))
    print(f"기록: {rdir}")


if __name__ == "__main__":
    sys.exit(main())
