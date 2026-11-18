#!/usr/bin/env python3
"""실행기 — 실행 1회 = (모델, 조건, 반복 번호). 단계 메시지를 한 턴씩 컨테이너 안의 `claude -p`로 재생하고 턴마다 기록한다.

사용:
  python3 tools/run_task.py --model claude-sonnet-5-5 --condition compact --rep 1 \
      [--last-step 8] [--turn-timeout 5400] [--max-tokens N] [--max-wall 36000] [--max-budget-usd X] \
      [--keep-root] [--allow-concurrent] [--dry-run] [--check-env [--check-gradle]] [--config tools/experiment.json]

격리(tools/README.md): 에이전트는 이미지 `cfg.image` 컨테이너(root 아닌 호스트 uid) 안에서 돈다. 호스트 파일 시스템은
작업 폴더 bind 하나(<run_root>/work → /work)와 읽기 전용 훅 폴더(<run_root>/hooks → /cx/hooks)만 보인다. HOME(~/.claude
세션 기록)과 훅 데이터는 실행 전용 Docker 볼륨이고, 테스트 DB는 실행 전용 네트워크의 PostgreSQL 16 사이드카다.
Docker 소켓·호스트 HOME·프로젝트 폴더는 마운트하지 않는다. 인증은 `--env-file`(토큰 파일)로만 넘긴다.

출력: <runs_dir>/<run id>/ — run.json(턴마다 갱신), settings.json, messages.json,
turns/NN/{result.json, stderr.txt, transcript.jsonl, boundaries.json, meta.json, container.json}, hookdata/,
work.tar.gz, claude-home.tar.gz.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containers  # noqa: E402
import transcript  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = ("control", "compact", "outbox")
COPY_IGNORE = shutil.ignore_patterns(".git", "build", ".gradle", ".kotlin", ".idea", "out")
LABEL = containers.EXPERIMENT_LABEL
EXIT = {"complete": 0, "aborted": 2, "failed": 3}
# 자동 메모리(auto memory) 끔 — 세 조건 같게. 켜져 있으면 에이전트가 대화 전용 제약을 ~/.claude/projects/<p>/memory/에 적어
# 컴팩트와 무관하게 남긴다(스모크에서 확인). 문서: https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory
# 설정 키와 환경 변수를 둘 다 둔다(둘 중 하나만으로도 꺼짐 — 2.1.326 이미지에서 확인, README).
AUTO_MEMORY_SETTINGS = {"autoMemoryEnabled": False}
AUTO_MEMORY_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


# ---------- 설정 ----------

def load_config(path=None):
    p = Path(path) if path else ROOT / "tools" / "experiment.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["_path"] = str(p)
    for k in ("task_dir", "skeleton_dir", "runs_dir"):
        cfg[k] = str(resolve(cfg[k]))
    for k in ("run_root", "token_env_file"):
        cfg[k] = str(Path(os.path.expanduser(cfg[k])))
    return cfg


def resolve(p):
    p = Path(os.path.expanduser(p))
    return p if p.is_absolute() else ROOT / p


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def messages_digest(hashes):
    """메시지 전체 해시 = 메시지별 sha256(16진 문자열)을 차례로 이은 것의 sha256."""
    h = hashlib.sha256()
    for x in hashes:
        h.update(x.encode())
    return h.hexdigest()


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


# ---------- 조건별 설정 (모든 경로는 컨테이너 안 경로) ----------

def hook_cmd(py, hooks_dir, script, *args):
    return " ".join([py, "-I", f"{hooks_dir}/{script}", *args])


def settings_for(condition, cfg):
    """작업 폴더 .claude/settings.json 내용(dict). 조건 간 차이는 outbox 훅 유무뿐."""
    if condition not in CONDITIONS:
        raise ValueError(f"알 수 없는 조건: {condition}")
    c = cfg["container"]
    py, hooks, data = c["hook_python"], c["hooks"], c["hookdata"]
    collect = [{"hooks": [{"type": "command", "command": hook_cmd(py, hooks, "compact_log.py", data), "timeout": 30}]}]
    hk = {"PreCompact": collect, "PostCompact": collect}
    if condition == "outbox":
        hk["UserPromptSubmit"] = [{"hooks": [{"type": "command", "timeout": 30,
                                              "command": hook_cmd(py, hooks, "capture.py", data)}]}]
        hk["SessionStart"] = [{"matcher": "compact", "hooks": [{
            "type": "command", "timeout": 30,
            "command": hook_cmd(py, hooks, "inject.py", data, f"{hooks}/outbox-config.json")}]}]
    file_tools = ("Read", "Edit", "Write", "Grep", "Glob")
    # HOME(~/.claude)은 막지 않는다 — CLI가 큰 도구 출력을 ~/.claude/projects/.../tool-results에 두고 에이전트가 Read로 읽는다
    deny = [f"{t}(/{d}/**)" for d in (hooks, data) for t in file_tools]
    deny += [f"{t}(/{c['work']}/.claude/**)" for t in ("Edit", "Write")]
    deny += list(cfg["forbidden_tools"])
    return {"permissions": {"allow": list(cfg["allowed_tools"]), "deny": deny,
                            "blockReadsOutsideWorkingDirectories": True,
                            "blockWritesOutsideWorkingDirectories": True},
            "hooks": hk, **AUTO_MEMORY_SETTINGS}


def settings_text(d):
    return json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def claude_cmd(cfg, model, session_id, first, budget=None):
    c = cfg["container"]
    cmd = [c["claude_bin"], "-p", "--model", model, "--output-format", "json",
           "--setting-sources", "project", "--strict-mcp-config",
           "--mcp-config", f"{c['hooks']}/empty-mcp.json",
           "--tools", ",".join(cfg["allowed_tools"]),
           "--permission-mode", cfg["permission_mode"]]
    cmd += ["--session-id", session_id] if first else ["--resume", session_id]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    return cmd


def container_env(cfg, condition):
    """에이전트 컨테이너 환경(토큰 제외 — 토큰은 --env-file로만). 조건 간 차이는 컴팩트 기준값뿐."""
    home = cfg["container"]["home"]
    return {"HOME": home, "CLAUDE_CONFIG_DIR": f"{home}/.claude", "DISABLE_AUTOUPDATER": "1", **AUTO_MEMORY_ENV,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": cfg["compact_window"][condition], **containers.jdbc_env(cfg["db"])}


# ---------- Docker 리소스 ----------

def resource_names(cfg, run_id):
    tag = re.sub(r"[^a-z0-9_.-]", "-", (cfg["resource_prefix"] + run_id).lower())
    return {"tag": tag, "net": f"{tag}-net", "db": f"{tag}-db", "pgdata": f"{tag}-pgdata",
            "home": f"{tag}-home", "hookdata": f"{tag}-hookdata", "init": f"{tag}-init", "collect": f"{tag}-collect"}


def turn_container(names, k):
    return f"{names['tag']}-t{k:02d}"


def uid_gid():
    return f"{os.getuid()}:{os.getgid()}"


def agent_create_args(cfg, names, root, condition, name, cmd):
    """턴 하나의 `docker create` 인자. 쓰기 가능한 호스트 경로는 작업 폴더 하나뿐."""
    c = cfg["container"]
    args = ["create", "--name", name, "--label", LABEL, "--label", f"cx.run={names['tag']}",
            "--interactive", "--init", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            f"--pids-limit={c['pids_limit']}", f"--cpus={c['cpus']}", f"--memory={c['memory']}",
            "--user", uid_gid(), "--network", names["net"], "--workdir", c["work"],
            "--env-file", cfg["token_env_file"]]
    for k, v in sorted(container_env(cfg, condition).items()):
        args += ["--env", f"{k}={v}"]
    args += ["--mount", f"type=bind,src={root}/work,dst={c['work']}",
             "--mount", f"type=bind,src={root}/hooks,dst={c['hooks']},readonly",
             "--mount", f"type=volume,src={names['home']},dst={c['home']}",
             "--mount", f"type=volume,src={names['hookdata']},dst={c['hookdata']}"]
    return args + [cfg["image"], *cmd]


def preflight(docker, cfg, names, allow_concurrent):
    """만들기 전에 확인: 이미지(uid 레이블), 같은 이름의 잔여 리소스, 이 실험의 다른 실행, 다른 실험의 실행 중 컨테이너."""
    info = docker.image_info(cfg["image"])
    want, got = uid_gid().split(":"), [info["labels"].get("cx.uid"), info["labels"].get("cx.gid")]
    if got != want:
        raise SystemExit(f"이미지 {cfg['image']}의 uid:gid {got} ≠ 호스트 {want} — --build-arg UID/GID로 다시 빌드")
    left = [f"{k} {n}" for k, n in planned_resources(names) if docker.exists(k, n)]
    if left:
        raise SystemExit(f"이전 실행의 Docker 잔여물: {left} — 확인 뒤 직접 지우고 다시 실행")
    mine = docker.names(all=True, label=LABEL)
    if mine:
        raise SystemExit(f"이 실험의 컨테이너가 이미 있다(한 번에 하나만): {mine}")
    busy = [n for n in docker.names() if n.startswith(tuple(cfg.get("refuse_when_running_prefixes", [])))]
    if busy and not allow_concurrent:
        raise SystemExit(f"다른 실험의 컨테이너가 돌고 있다(명세 §2 — 동시 실행 금지): {busy}")
    return info


def planned_resources(names):
    return [("container", names["db"]), ("container", names["init"]), ("container", names["collect"]),
            ("network", names["net"]), ("volume", names["pgdata"]), ("volume", names["home"]),
            ("volume", names["hookdata"])]


def setup_stack(docker, cfg, names, created):
    """네트워크·볼륨·DB 사이드카를 만들고 HOME 볼륨을 초기화. 만든 것은 즉시 created에 적는다(정리용)."""
    c = cfg["container"]
    created.append(("network", names["net"]))
    docker.create_network(names["net"], names["tag"])
    for v in ("pgdata", "home", "hookdata"):
        created.append(("volume", names[v]))
        docker.create_volume(names[v], names["tag"])
    created.append(("container", names["db"]))
    containers.start_postgres(docker, cfg["db"], names["db"], names["net"], names["tag"],
                              f"type=volume,src={names['pgdata']},dst=/var/lib/postgresql/data")
    sc = cfg["skeleton_commit"]
    script = " && ".join(['mkdir -p "$CLAUDE_CONFIG_DIR"',
                          f"git config --global user.name {shlex.quote(sc['name'])}",
                          f"git config --global user.email {shlex.quote(sc['email'])}",
                          "git config --global init.defaultBranch main"])
    created.append(("container", names["init"]))
    docker.check(docker.run(
        "run", "--rm", "--name", names["init"], "--label", LABEL, "--network", "none", "--user", uid_gid(),
        "--env", f"HOME={c['home']}", "--env", f"CLAUDE_CONFIG_DIR={c['home']}/.claude",
        "--mount", f"type=volume,src={names['home']},dst={c['home']}",
        "--mount", f"type=volume,src={names['hookdata']},dst={c['hookdata']}",
        cfg["image"], "sh", "-c", script, timeout=300), "HOME 볼륨 초기화")


def cleanup_stack(docker, created):
    """만든 것을 역순으로 지운다(정확한 이름만). 오류 목록."""
    errors = []
    for kind in ("container", "network", "volume"):
        for k, n in reversed(created):
            if k == kind:
                err = docker.remove(k, n)
                if err:
                    errors.append(err)
    return errors


# ---------- 토큰(값은 출력·기록하지 않는다) ----------

def token_file_status(path):
    """dry-run·check-env용 — 내용은 읽지 않는다."""
    p = Path(path)
    if not p.is_file():
        return {"path": str(p), "exists": False, "mode": None}
    return {"path": str(p), "exists": True, "mode": oct(stat.S_IMODE(p.stat().st_mode))}


def read_token(path, key):
    """토큰 파일 형식 확인 후 값(검사용 바늘)만 돌려준다: 저장소 밖 · 권한 600 · `KEY=값` 한 줄."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"토큰 파일 없음: {p}")
    if ROOT in p.resolve().parents:
        raise ValueError("토큰 파일은 저장소 밖에 있어야 한다")
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        raise ValueError("토큰 파일 권한이 느슨하다(chmod 600)")
    if "," in str(p.resolve()):
        raise ValueError("토큰 파일 경로에 쉼표가 있다")
    lines = [x for x in p.read_text().splitlines() if x.strip() and not x.lstrip().startswith("#")]
    k, _, v = (lines[0] if len(lines) == 1 else "").partition("=")
    if len(lines) != 1 or k.strip() != key or not v.strip():
        raise ValueError(f"토큰 파일에는 {key}=<값> 한 줄만 있어야 한다")
    return v.strip()


SCAN_CHUNK = 1 << 20


def file_contains(path, needle):
    tail, overlap = b"", len(needle) - 1
    with open(path, "rb") as f:
        while True:
            chunk = f.read(SCAN_CHUNK)
            if not chunk:
                return False
            if needle in tail + chunk:
                return True
            tail = (tail + chunk)[-overlap:] if overlap else b""


def scrub(base, secret, skip_suffixes=()):
    """base 아래 모든 파일(이름 포함)에서 secret을 찾아 `<REDACTED-SECRET>`로 바꾼다. 찾은 상대 경로 목록."""
    base, needle, hits = Path(base), secret.encode(), []
    if not base.exists():
        return hits
    for f in sorted(base.rglob("*"), key=lambda p: -len(p.parts)):
        if f.is_symlink():
            continue
        if secret in f.name:
            target = f.with_name(f.name.replace(secret, "REDACTED-SECRET"))
            f.rename(target)
            hits.append(str(target.relative_to(base)))
            f = target
        if f.is_file() and not f.name.endswith(tuple(skip_suffixes)) and file_contains(f, needle):
            f.write_bytes(f.read_bytes().replace(needle, b"<REDACTED-SECRET>"))
            hits.append(str(f.relative_to(base)))
    return sorted(set(hits))


def git_objects_hits(work, secret):
    """작업 폴더 git 저장소의 모든 객체(느슨한 객체·팩, 압축을 푼 내용)에서 secret을 찾는다 — 바이트 검사(scrub)는 zlib으로
    압축된 `.git/objects`를 볼 수 없다. 찾으면 [".git"], 없거나 저장소가 없으면 []. git 실패는 예외(검사 실패로 처리)."""
    work, needle = Path(work), secret.encode()
    if not (work / ".git").is_dir():
        return []
    p = subprocess.Popen(["git", "-c", f"safe.directory={work}", "cat-file", "--batch-all-objects", "--batch", "--unordered"],
                         cwd=work, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    tail, overlap, found = b"", len(needle) - 1, False
    while not found:
        chunk = p.stdout.read(SCAN_CHUNK)
        if not chunk:
            break
        found = needle in tail + chunk
        tail = (tail + chunk)[-overlap:] if overlap else b""
    p.stdout.close()
    if found:
        p.kill()
    rc = p.wait()
    if not found and rc != 0:
        raise RuntimeError(f"git cat-file --batch-all-objects 실패(rc={rc})")
    return [".git"] if found else []


# ---------- 준비 ----------

def git(args, cwd, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


def stage_hooks(dest, condition):
    """훅 스크립트·설정을 dest(→ 컨테이너 /cx/hooks, 읽기 전용)로 복사."""
    tools = ROOT / "tools"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    shutil.copy2(tools / "compact_log.py", dest / "compact_log.py")
    if condition == "outbox":
        shutil.copy2(tools / "outbox" / "capture.py", dest / "capture.py")
        shutil.copy2(tools / "outbox" / "inject.py", dest / "inject.py")
        shutil.copy2(tools / "outbox" / "config.json", dest / "outbox-config.json")


def prepare_root(cfg, condition, settings):
    r = Path(cfg["run_root"])
    if r.exists():
        raise SystemExit(f"{r} 가 이미 있다 — 이전 실행의 잔여물. 내용을 확인한 뒤 직접 지우고 다시 실행.")
    for d in ("hooks", "work", "scratch"):
        (r / d).mkdir(parents=True)
    os.chmod(r, 0o700)
    stage_hooks(r / "hooks", condition)
    sc = cfg["skeleton_commit"]
    work = r / "work"
    skel = Path(cfg["skeleton_dir"])
    if not (skel / "build.gradle.kts").is_file():
        raise SystemExit(f"골격이 없다: {skel}")
    shutil.copytree(skel, work, dirs_exist_ok=True, ignore=COPY_IGNORE)
    # 호스트 git 설정을 읽지 않고 이름·시각을 고정 → 실행마다 같은 SHA
    genv = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME=sc["name"], GIT_AUTHOR_EMAIL=sc["email"], GIT_AUTHOR_DATE=sc["date"],
                GIT_COMMITTER_NAME=sc["name"], GIT_COMMITTER_EMAIL=sc["email"], GIT_COMMITTER_DATE=sc["date"])
    git(["init", "-q", "-b", "main"], work, genv)
    git(["add", "-A"], work, genv)
    git(["commit", "-q", "-m", sc["message"]], work, genv)
    with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.write(".claude/\n")
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(settings, encoding="utf-8")
    return git(["rev-parse", "HEAD"], work, genv).stdout.strip()


# ---------- 감사·무효 ----------

def audit_patterns(cfg):
    c = cfg["container"]
    return [p.format(real_home=str(Path.home()), hooks=c["hooks"], hookdata=c["hookdata"])
            for p in cfg["audit_forbidden_patterns"]]


def audit(entries, cfg):
    """도구 입력에서 금지 경로·금지 도구. [{line, tool, hit}]"""
    pats, bad_tools, out = audit_patterns(cfg), set(cfg["forbidden_tools"]), []
    for line, name, inp in transcript.tool_uses(entries):
        if name in bad_tools:
            out.append({"line": line, "tool": name, "hit": f"tool:{name}"})
        text = json.dumps(inp, ensure_ascii=False)
        out += [{"line": line, "tool": name, "hit": p} for p in pats if p in text]
    return out


# 원문 재독 경로(공변량 — 막지 않고 기록한다). 도구 입력 문자열에 대한 패턴 검사라 우회·오탐이 있다(README).
TOOL_RESULTS = re.compile(r"[^\s\"']*/tool-results/[^\s\"']*")  # CLI가 큰 도구 출력을 두는 곳 — 정상 경로라 지운 뒤 본다
# CLI의 백그라운드 작업 출력 파일 /tmp/claude-<uid>/<프로젝트>/<세션 id>/tasks/<id>.output — 세션 id가 들어 있지만 세션 기록이
# 아니다(파일럿 Sonnet compact 실행의 `tail -30 …/tasks/….output`). tool-results와 같이 지운 뒤 본다
TASK_OUTPUT = re.compile(r"[^\s\"']*/tmp/claude-\d+/[^\s\"'/]+/[^\s\"'/]+/tasks/[^\s\"'/]+\.output\b")
MEMO_NAME = (r"(?:CLAUDE|AGENTS)\.md|[^\s/'\"]*(?:notes?|decisions?|memo|rules?|constraints?|conventions?|guidelines?"
             r"|규칙|결정|메모|제약|노트|지침)[^\s/'\"]*\.(?:md|txt|markdown|rst)")
REREAD = {
    # (가) 세션 기록 폴더: ~/.claude(projects 등), *.jsonl 전역 탐색, 이 세션 id
    "session_log": r"\.claude/projects|(?:~|/home/agent|\$\{?HOME\}?)/\.claude\b|CLAUDE_CONFIG_DIR|\*[^\s\"']*\.jsonl",
    # (나) 아웃박스 데이터(훅 데이터 볼륨)
    "outbox_data": r"hookdata|outbox/[^\s\"']*\.jsonl|inject-log",
    # (다) 루트·HOME·/cx 전역 탐색
    "root_search": r"(?:^|[\s;&|(])(?:find|grep|egrep|rg|ls|du|tree|fd|locate)\b[^\n;&|]*?"
                   r"\s(?:/|/cx|/home|/home/agent|~|\$\{?HOME\}?)/?(?=[\s;&|)]|$)",
    # (라) 작업 폴더의 자기 메모: CLAUDE.md·AGENTS.md, 작업 폴더 .claude/ 아래 새 파일, 이름에 notes·decisions·규칙 등이 든 문서
    "memo": r"(?:^|[\s'\"=>]|/work/|\./)\.claude/(?!settings\.json)[^\s'\"]+|(?:^|[\s/'\"=>])(?i:" + MEMO_NAME + ")",
}
ROOT_PATHS = ("/", "/cx", "/home", "/home/agent", "~")
COLLECTION_DATA = re.compile(r"compact-log|hook-errors")  # 수집 전용(요약문·프롬프트 앞부분이 든다) — 접근하면 무효


def tool_text(inp):
    """도구 입력의 문자열 값들을 줄로 이은 것(JSON 이스케이프 없이 — 명령 속 줄바꿈이 공백으로 보이게)."""
    return "\n".join(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in inp.values())


def reread_hits(entries, start, end, session_id):
    """[start, end) 줄의 도구 입력에서 원문 재독 경로(가~라). {종류: [{line, tool, match}]}"""
    pats = {k: re.compile(v + (f"|{re.escape(session_id)}" if k == "session_log" else "")) for k, v in REREAD.items()}
    out = {k: [] for k in REREAD}
    for line, name, inp in transcript.tool_uses(entries, start, end):
        text = TASK_OUTPUT.sub(" ", TOOL_RESULTS.sub(" ", tool_text(inp)))
        for kind, pat in pats.items():
            m = pat.search(text)
            if m:
                out[kind].append({"line": line, "tool": name, "match": m.group(0).strip()[:120]})
        if name in ("Grep", "Glob") and str(inp.get("path") or "").rstrip("/") in [x.rstrip("/") for x in ROOT_PATHS]:
            out["root_search"].append({"line": line, "tool": name, "match": f"path={inp.get('path')}"})
    return out


def collection_data_reads(entries):
    return [{"line": line, "tool": name, "match": m.group(0)} for line, name, inp in transcript.tool_uses(entries)
            for m in [COLLECTION_DATA.search(json.dumps(inp, ensure_ascii=False))] if m]


def unexpected_tools(entries, cfg):
    allowed = set(cfg["allowed_tools"]) | set(cfg["forbidden_tools"])
    return sorted({name for _, name, _ in transcript.tool_uses(entries) if name not in allowed})


def _ts(x):
    try:
        return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None


def boundary_injections(compact_log, injections, n_bounds):
    """경계마다 그 경계의 주입(차례대로). 경계 k의 창 = [k번째 PreCompact 시각, k+1번째 PreCompact 시각) — 둘 다 같은 컨테이너
    안의 훅이 남긴 시각(수집 훅 compact-log·주입 훅 inject-log)이라 시계가 같다. 창 안의, 앞 경계에 쓴 주입보다 뒤의 첫 주입을
    잇는다. [{boundary, injection(inject-log 차례 또는 None)}]"""
    pre = [_ts(r.get("ts")) for r in compact_log if r.get("event") == "PreCompact"]
    times = [(j, _ts(e.get("ts"))) for j, e in enumerate(injections)]
    out, last = [], -1
    for k in range(n_bounds):
        lo = pre[k] if k < len(pre) else None
        hi = pre[k + 1] if k + 1 < len(pre) else None
        j = None if lo is None else next((j for j, t in times if j > last and t is not None and t >= lo
                                          and (hi is None or t < hi)), None)
        if j is not None:
            last = j
        out.append({"boundary": k, "injection": j})
    return out


def auto_memory_files(projects):
    """턴마다 복사한 `~/.claude/projects` 사본에서 자동 메모리 파일(`<프로젝트>/memory/` 아래 파일) 목록 — 꺼져 있으면 비어야 한다.
    (켜진 CLI는 빈 `memory/` 폴더를 미리 만들기도 하므로 폴더가 아니라 파일을 본다.)"""
    projects = Path(projects)
    return sorted(str(f.relative_to(projects)) for f in projects.glob("*/memory/**/*") if f.is_file()) \
        if projects.is_dir() else []


def invalid_reasons(condition, bounds, audit_hits, settings_tampered, injections, compact_log=(), hook_errors=(),
                    transcript_unparsed=False, collection_reads=(), memory_files=()):
    out = []
    if condition == "control" and bounds:
        out.append("control_has_compaction")
    if condition != "control" and not bounds:
        out.append("no_compaction")
    out += sorted({f"audit:{h['hit']}" for h in audit_hits})
    if settings_tampered:
        out.append("settings_tampered")
    if transcript_unparsed:  # 읽지 못한 줄이 경계였을 수 있다 → 경계 수를 믿을 수 없다
        out.append("transcript_unparsed")
    for event in ("PreCompact", "PostCompact"):  # 수집 훅이 본 컴팩트 횟수 = 세션 기록의 경계 수
        if sum(r.get("event") == event for r in compact_log) != len(bounds):
            out.append("compact_hook_count_mismatch")
            break
    if condition == "outbox":
        if len(injections) != len(bounds):
            out.append("outbox_injection_count_mismatch")
        if any(x["injection"] is None for x in boundary_injections(compact_log, injections, len(bounds))):
            out.append("outbox_boundary_without_injection")  # 경계마다 그 경계의 주입이 있어야 한다(수만 같아도 안 됨)
        if any(e.get("hook") == "capture" for e in hook_errors):
            out.append("outbox_capture_error")
    if collection_reads:
        out.append("collection_data_read")
    if memory_files:  # 자동 메모리를 껐는데 메모리 파일이 생김 — 제약이 컴팩트와 무관하게 파일로 남았을 수 있다
        out.append("auto_memory_files")
    return out


# ---------- 턴 ----------

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# run.json 표식: 턴의 tokens·cost_usd가 "앞 턴 대비 차분"인 기록. 없으면 옛 형식(턴 값 = 세션 누적 — summarize가 다시 계산)
USAGE_ACCOUNTING = "session_cumulative_delta"


def usage_tokens(res):
    """결과의 `usage` 합 — 그 호출의 주 루프 요청만(컴팩트 요약·서브에이전트 등은 빠짐). 없으면 None."""
    u = res.get("usage") or {}
    return sum(u.get(k) or 0 for k in USAGE_KEYS) if u else None


def tokens_of(res):
    """결과의 `modelUsage` 합(없으면 `usage` 합). CLI 2.1.277+의 `--resume` 호출에서는 **세션 처음부터의 누적**이다."""
    mu = res.get("modelUsage") or {}
    if mu:
        return sum((v.get("inputTokens") or 0) + (v.get("outputTokens") or 0) + (v.get("cacheReadInputTokens") or 0)
                   + (v.get("cacheCreationInputTokens") or 0) for v in mu.values())
    return usage_tokens(res)


def turn_spend(cum_tokens, cum_cost, session, prev):
    """턴의 토큰·비용 = 결과의 누적값(modelUsage 합·total_cost_usd)의 앞 턴 대비 차분.
    prev = 앞 턴까지의 {session, tokens, cost}(마지막으로 알려진 누적값) 또는 None(첫 턴).
    첫 턴·세션이 바뀐 턴(결과 session_id가 앞과 다름)·누적이 줄어든 턴은 차분하지 않고 그 값을 그대로 쓴다(basis로 표시).
    돌려줌: ({tokens, cost_usd, basis}, 경고 문자열 목록)."""
    warn = []
    if prev is None:
        basis = "first"
    elif session is not None and prev.get("session") is not None and session != prev["session"]:
        basis = "session_changed"
        warn.append(f"결과 session_id가 앞 턴과 다름({prev['session']} → {session}) — 누적 차분 대신 그 턴 값을 그대로 씀")
    else:
        basis = "delta"

    def one(cur, before, what):
        if cur is None:
            return None
        if basis != "delta" or before is None:
            return cur
        if cur < before:
            warn.append(f"{what} 누적이 줄어듦({before} → {cur}) — 차분 대신 그 턴 값을 그대로 씀")
            return cur
        return cur - before
    tok = one(cum_tokens, (prev or {}).get("tokens"), "토큰")
    cost = one(cum_cost, (prev or {}).get("cost"), "비용")
    if any("줄어듦" in w for w in warn):
        basis = "decreased"
    return {"tokens": tok, "cost_usd": None if cost is None else round(cost, 6), "basis": basis}, warn


def recompute_spend(turns):
    """옛 형식 기록(턴의 tokens·cost_usd = 세션 누적)에서 턴별 차분과 합을 다시 낸다. (턴별 [{tokens, cost_usd, basis}], 경고,
    {tokens, cost_usd} 합 — 모르는 턴이 있으면 그 합은 None)."""
    out, warns, prev = [], [], None
    for t in turns:
        sp, w = turn_spend(t.get("tokens"), t.get("cost_usd"), t.get("result_session_id"), prev)
        out.append(sp)
        warns += [f"turn {t.get('index')}: {x}" for x in w]
        prev = {"session": t.get("result_session_id") or (prev or {}).get("session"),
                "tokens": t.get("tokens") if t.get("tokens") is not None else (prev or {}).get("tokens"),
                "cost": t.get("cost_usd") if t.get("cost_usd") is not None else (prev or {}).get("cost")}
    tok = [x["tokens"] for x in out]
    cost = [x["cost_usd"] for x in out]
    return out, warns, {"tokens": sum(tok) if tok and None not in tok else None,
                        "cost_usd": round(sum(cost), 4) if cost and None not in cost else None}


def run_spend(run):
    """실행의 {tokens, cost_usd}와 다시 계산했는지. 새 형식(usage_accounting 표식)은 totals 그대로, 없으면 옛 형식으로 보고
    턴의 원시 값(누적)에서 차분 합을 다시 낸다."""
    if run.get("usage_accounting") == USAGE_ACCOUNTING:
        t = run.get("totals") or {}
        return {"tokens": t.get("tokens"), "cost_usd": t.get("cost_usd")}, False
    return recompute_spend(run.get("turns") or [])[2], True


def context_tokens_last(res):
    it = (res.get("usage") or {}).get("iterations") or []
    if not it:
        return None
    last = it[-1]
    return sum(last.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def tracked_diff_sha256(work):
    """추적 파일의 작업 트리 변경(`git diff HEAD --binary`)의 sha256 — 턴 사이에 추적 파일이 바뀌었는지 비교용. 실패면 None."""
    p = subprocess.run(["git", "-c", f"safe.directory={work}", "diff", "HEAD", "--binary"], cwd=work, capture_output=True)
    return sha256_bytes(p.stdout) if p.returncode == 0 else None


def run_turn(docker, name, msg, timeout, out_path, err_path):
    """만들어 둔 컨테이너를 붙어서 시작하고 메시지를 표준 입력으로 보낸다. (exit_code, timed_out, wall_s)"""
    t0 = time.time()
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        p = subprocess.Popen([docker.bin, "start", "--attach", "--interactive", name], stdin=subprocess.PIPE,
                             stdout=fo, stderr=fe, start_new_session=True)
        try:
            p.communicate(msg.encode("utf-8"), timeout=timeout)
            return p.returncode, False, time.time() - t0
        except subprocess.TimeoutExpired:
            docker.run("kill", name)  # docker 클라이언트만 죽이면 데몬 쪽 컨테이너는 계속 돈다
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


def collect_turn(docker, cfg, name, session_id, scratch, rdir):
    """끝난 턴 컨테이너에서 세션 기록과 훅 데이터를 꺼낸다. (transcript 경로 또는 None, 문제 목록)"""
    c, problems = cfg["container"], []
    proj = scratch / "projects"
    shutil.rmtree(proj, ignore_errors=True)
    p = docker.run("cp", f"{name}:{c['home']}/.claude/projects", str(proj), timeout=600)
    if p.returncode != 0:
        problems.append(f"세션 기록 복사 실패(docker cp rc={p.returncode}): {p.stderr.strip()[:200]}")
    hits = sorted(proj.glob(f"*/{session_id}.jsonl")) if proj.is_dir() else []
    (rdir / "hookdata").mkdir(exist_ok=True)
    h = docker.run("cp", f"{name}:{c['hookdata']}/.", str(rdir / "hookdata"), timeout=300)
    if h.returncode != 0:
        problems.append(f"훅 데이터 복사 실패(docker cp rc={h.returncode}): {h.stderr.strip()[:200]}")
    return (hits[0] if hits else None), problems


def read_jsonl(p):
    p = Path(p)
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_json(p, obj):
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def archive_dir(src, dest, arcname, skip=("build", ".gradle", ".kotlin")):
    def flt(ti):
        return None if any(x in skip for x in Path(ti.name).parts) else ti
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src, arcname=arcname, filter=flt)


# ---------- 본체 ----------

def model_short(model):
    return re.sub(r"[^A-Za-z0-9.-]", "", model.replace("claude-", ""))


def make_run_id(model, condition, rep):
    return f"{model_short(model)}-{condition}-r{rep}-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}"


def dry_run(cfg, a):
    settings = settings_text(settings_for(a.condition, cfg))
    msgs = build_messages(cfg, a.last_step)
    run_id = make_run_id(a.model, a.condition, a.rep)
    names = resource_names(cfg, run_id)
    docker = containers.Docker.from_cfg(cfg)
    root, sid = cfg["run_root"], "<session-uuid>"
    print(f"run_id(예): {run_id}")
    print(f"run_root: {root}  work: {root}/work → {cfg['container']['work']}  기록: {cfg['runs_dir']}/<run id>/")
    print(f"골격: {cfg['skeleton_dir']} ({'있음' if Path(cfg['skeleton_dir'], 'build.gradle.kts').is_file() else '없음'})")
    try:
        info = docker.image_info(cfg["image"])
        print(f"이미지: {cfg['image']} id={info['id']} labels={info['labels']}")
    except (containers.DockerError, OSError) as e:
        print(f"이미지: {cfg['image']} — 확인 실패: {e}")
    print("토큰 파일(경로·권한만, 내용은 읽지 않음):", token_file_status(cfg["token_env_file"]))
    print("Docker 리소스(이 실행 전용):", {k: v for k, v in names.items() if k != "tag"})
    print(f"settings.json sha256={sha256_bytes(settings.encode())}")
    print(settings, end="")
    print("컨테이너 env(토큰 제외):", container_env(cfg, a.condition))
    print("DB 사이드카:", "docker " + " ".join(containers.postgres_args(
        cfg["db"], names["db"], names["net"], names["tag"], f"type=volume,src={names['pgdata']},dst=/var/lib/postgresql/data")))
    first = agent_create_args(cfg, names, root, a.condition, turn_container(names, 0),
                              claude_cmd(cfg, a.model, sid, True, a.max_budget_usd))
    later = claude_cmd(cfg, a.model, sid, False, a.max_budget_usd)
    print("턴 0: docker " + " ".join(first))
    print(f"      docker start --attach --interactive {turn_container(names, 0)} < 메시지 0")
    print("턴 k: 같은 create 인자(이름 -tNN), 명령만:", " ".join(later), "< 메시지 k")
    for k, m in enumerate(msgs):
        print(f"메시지 {k} ({cfg['steps'][k]}): {len(m)}자 sha256={sha256_bytes(m.encode())}")
    print(f"메시지 전체 sha256={messages_digest([sha256_bytes(m.encode()) for m in msgs])}")
    if a.condition == "outbox":
        oc = (ROOT / "tools" / "outbox" / "config.json").read_bytes()
        print(f"outbox config sha256={sha256_bytes(oc)}: {oc.decode().strip()}")
    return 0


PROBE = r"""
claude --version
java -version 2>&1 | head -1
git --version
python3 --version
echo "id=$(id -u):$(id -g) user=$(id -un) HOME=$HOME"
for p in __HOST_PATHS__ /var/run/docker.sock /run/docker.sock; do
  if [ -e "$p" ]; then echo "VISIBLE $p"; else echo "absent $p"; fi
done
echo "--- mounts(/work·/cx·/home·/var/run·/run)"
awk '$2 ~ /^\/(work|cx|home|var\/run|run)/ {print $2, $3, $4}' /proc/self/mounts | cut -c1-120
echo "--- hooks(fake input)"
echo '{"session_id":"probe","hook_event_name":"PreCompact","trigger":"auto"}' | python3 -I /cx/hooks/compact_log.py /cx/hookdata
echo '{"session_id":"probe","prompt":"hello"}' | python3 -I /cx/hooks/capture.py /cx/hookdata
echo '{"session_id":"probe","source":"compact"}' | python3 -I /cx/hooks/inject.py /cx/hookdata /cx/hooks/outbox-config.json | cut -c1-160
echo
ls /cx/hookdata /cx/hookdata/outbox
if [ -s /cx/hookdata/hook-errors.jsonl ]; then echo HOOK_ERRORS; cat /cx/hookdata/hook-errors.jsonl; fi
"""


def check_env(cfg, with_gradle):
    """유료 호출 없이: 이미지·토큰 파일(경로·권한)·컨테이너 안 도구 버전·격리(호스트 경로·Docker 소켓)·훅 실행,
    (선택) 실행 전용 네트워크 + PostgreSQL 사이드카에서 골격 `./gradlew test --offline` 두 번."""
    docker, ok = containers.Docker.from_cfg(cfg), True

    def show(label, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else 'FAIL'}] {label}: {detail}")

    try:
        info = docker.image_info(cfg["image"])
    except (containers.DockerError, OSError) as e:
        show("이미지", False, str(e))
        return 1
    show("이미지", True, f"{cfg['image']} id={info['id']} size={info['size']} labels={info['labels']}")
    show("이미지 uid:gid = 호스트", [info["labels"].get("cx.uid"), info["labels"].get("cx.gid")] == uid_gid().split(":"),
         f"{info['labels'].get('cx.uid')}:{info['labels'].get('cx.gid')} vs {uid_gid()}")
    tf = token_file_status(cfg["token_env_file"])
    show("토큰 파일(경로·권한만 — 내용은 읽지 않음)", tf["exists"] and tf["mode"] == "0o600", tf)
    tag = f"{cfg['resource_prefix']}envcheck-{uuid.uuid4().hex[:8]}"
    base = Path(tempfile.mkdtemp(prefix="cx-envcheck-"))
    c = cfg["container"]
    try:
        stage_hooks(base / "hooks", "outbox")
        (base / "work").mkdir()
        host_paths = " ".join(shlex.quote(p) for p in (str(Path.home()), str(Path.home() / "project"), str(ROOT)))
        p = docker.run("run", "--rm", "--name", f"{tag}-probe", "--label", LABEL, "--network", "none",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", uid_gid(),
                       "--mount", f"type=bind,src={base}/work,dst={c['work']}",
                       "--mount", f"type=bind,src={base}/hooks,dst={c['hooks']},readonly",
                       "--mount", f"type=tmpfs,dst={c['hookdata']},tmpfs-mode=1777",
                       cfg["image"], "sh", "-c", PROBE.replace("__HOST_PATHS__", host_paths), timeout=300)
        out = p.stdout + p.stderr
        print(out.rstrip())
        show("프로브 종료 코드", p.returncode == 0, p.returncode)
        show("claude --version = 2.1.326", "2.1.326" in out.splitlines()[0] if out else False, out.splitlines()[:1])
        show("호스트 경로·Docker 소켓이 보이지 않음", "VISIBLE" not in out, "")
        show("훅 3개 실행(오류 없음, 주입 출력 있음)", "HOOK_ERRORS" not in out and "hookSpecificOutput" in out
             and "compact-log.jsonl" in out, "")
        if with_gradle:
            ok &= check_gradle(cfg, docker, tag, base, show)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0 if ok else 1


def check_gradle(cfg, docker, tag, base, show):
    c, good = cfg["container"], True
    w = base / "skeleton"
    shutil.copytree(cfg["skeleton_dir"], w, ignore=COPY_IGNORE)
    with containers.PostgresStack(docker, cfg["db"], tag, internal=True) as st:
        for i in (1, 2):
            t0 = time.time()
            env = sum((["--env", f"{k}={v}"] for k, v in containers.jdbc_env(cfg["db"]).items()), [])
            p = docker.run("run", "--rm", "--name", f"{tag}-gradle{i}", "--label", LABEL, "--network", st.network,
                           "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", uid_gid(),
                           "--mount", f"type=bind,src={w},dst={c['work']}", "--workdir", c["work"], *env,
                           cfg["image"], "./gradlew", "test", "--rerun", "--offline", "--no-daemon",
                           "--console=plain", timeout=1800)
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-6:])
            show(f"골격 ./gradlew test --rerun --offline #{i}(내부 전용 네트워크 + 사이드카, {time.time() - t0:.0f}s)",
                 p.returncode == 0, "\n" + tail)
            good &= p.returncode == 0
            q = st.psql(cfg["db"]["name"], "-At", "-c",
                        "SELECT string_agg(datname, ',') FROM pg_database WHERE datname LIKE 'test\\_%'")
            h = st.psql(cfg["db"]["name"], "-At", "-c", "SELECT to_regclass('flyway_schema_history') IS NULL")
            print(f"  실행 #{i} 뒤 test_* 데이터베이스: {q.stdout.strip()} · 기본 DB({cfg['db']['name']})에 "
                  f"flyway_schema_history 없음: {h.stdout.strip()}")
    show("사이드카·네트워크 정리", not st.cleanup_errors, st.cleanup_errors)
    return good and not st.cleanup_errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--condition", choices=CONDITIONS, default="compact")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--last-step", type=int, default=None, help="이 단계 번호까지만 보냄(스모크용, 기본 전체)")
    ap.add_argument("--turn-timeout", type=int, default=5400, help="턴 하나의 시간 상한(초)")
    ap.add_argument("--max-tokens", type=int, default=None, help="실행 전체 토큰 합 상한(modelUsage 합)")
    ap.add_argument("--max-wall", type=int, default=None, help="실행 전체 시간 상한(초)")
    ap.add_argument("--max-budget-usd", default=None, help="턴마다 claude --max-budget-usd 로 넘김(호출 하나의 상한 — --resume로 되살린 앞 턴 비용은 세지 않음, README)")
    ap.add_argument("--keep-root", action="store_true", help="끝난 뒤 run_root(호스트 작업 폴더)를 지우지 않음")
    ap.add_argument("--allow-concurrent", action="store_true", help="다른 실험(payment-*) 컨테이너가 돌아도 실행")
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
    names = resource_names(cfg, run_id)
    rdir = Path(cfg["runs_dir"]) / run_id
    root = Path(cfg["run_root"])
    work, scratch = root / "work", root / "scratch"
    docker = containers.Docker.from_cfg(cfg)
    if root.exists():
        raise SystemExit(f"{root} 가 이미 있다 — 이전 실행의 잔여물. 내용을 확인한 뒤 직접 지우고 다시 실행.")
    token = read_token(cfg["token_env_file"], cfg["token_env_key"])  # 검사용 — 출력·기록하지 않는다
    image = preflight(docker, cfg, names, a.allow_concurrent)
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
    session_id = str(uuid.uuid4())
    env = container_env(cfg, a.condition)
    state = {
        "run_id": run_id, "model": a.model, "condition": a.condition, "rep": a.rep, "status": "running",
        "measurement_complete": False,
        "abort_reason": None, "failure_reasons": [], "invalid": None, "invalid_reasons": [], "warnings": [],
        "started_at": now_iso(), "ended_at": None, "cli_version": None, "session_id": session_id,
        "image": cfg["image"], "image_id": image["id"], "image_labels": image["labels"],
        "docker": {k: v for k, v in names.items()}, "token_env_file": cfg["token_env_file"],
        "run_root": str(root), "work_dir": cfg["container"]["work"], "skeleton_commit": skeleton_head,
        "steps_planned": cfg["steps"][:len(msgs)], "compact_window_env": env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
        "settings_sha256": settings_sha, "container_env": env,
        "auto_memory": {"settings": AUTO_MEMORY_SETTINGS, "env": AUTO_MEMORY_ENV}, "container_limits": {
            k: cfg["container"][k] for k in ("cpus", "memory", "pids_limit")},
        "outbox_config": None, "outbox_config_sha256": None,
        "messages_sha256": [sha256_bytes(m.encode()) for m in msgs],
        "messages_sha256_all": messages_digest([sha256_bytes(m.encode()) for m in msgs]),
        "limits": {"turn_timeout": a.turn_timeout, "max_tokens": a.max_tokens, "max_wall": a.max_wall,
                   "max_budget_usd": a.max_budget_usd},
        "turns": [], "boundaries": [], "totals": {"tokens": None, "cost_usd": None, "wall_s": 0.0},
        "usage_accounting": USAGE_ACCOUNTING,
        "audit": [], "unexpected_tools": [], "injections": [], "compact_log": [], "hook_errors": [],
        "collection_data_reads": [], "transcript_unparsed_turns": [], "auto_memory_files": [], "cleanup_errors": [],
        "secret_scan": None,
    }
    if a.condition == "outbox":
        oc = (root / "hooks" / "outbox-config.json").read_bytes()
        state["outbox_config"] = json.loads(oc)
        state["outbox_config_sha256"] = sha256_bytes(oc)
    write_json(rdir / "run.json", state)
    created, entries, tampered = [], [], False
    line_ends, prev_size, prev_hash = [], 0, None
    t_start, tokens_total, cost_total, tokens_known, cost_known = time.time(), 0, 0.0, True, True
    spend_prev = None  # 앞 턴까지의 마지막 누적값 {session, tokens, cost}
    try:
        setup_stack(docker, cfg, names, created)
        v = docker.run("run", "--rm", "--name", f"{names['tag']}-version", "--label", LABEL, "--network", "none",
                       "--user", uid_gid(), cfg["image"], cfg["container"]["claude_bin"], "--version")
        state["cli_version"] = v.stdout.strip() or None
        for k, msg in enumerate(msgs):
            tdir = rdir / "turns" / f"{k:02d}"
            tdir.mkdir()
            timeout, wall_limited = a.turn_timeout, False
            if a.max_wall:
                left = a.max_wall - (time.time() - t_start)
                if left <= 0:
                    state["status"], state["abort_reason"] = "aborted", f"time_cap before turn {k}"
                    break
                if left < timeout:
                    timeout, wall_limited = int(left), True
            name = turn_container(names, k)
            created.append(("container", name))
            docker.check(docker.run(*agent_create_args(cfg, names, root, a.condition, name,
                                                       claude_cmd(cfg, a.model, session_id, k == 0, a.max_budget_usd))),
                         f"create {name}")
            started = time.time()
            rc, timed_out, wall = run_turn(docker, name, msg, timeout, tdir / "result.json", tdir / "stderr.txt")
            ended = time.time()
            cstate = docker.container_state(name)
            write_json(tdir / "container.json", {"name": name, "state": cstate, "start_exit_code": rc})
            tpath, problems = collect_turn(docker, cfg, name, session_id, scratch, rdir)
            err = docker.remove("container", name)
            if err:
                state["cleanup_errors"].append(err)
            state["warnings"] += [f"turn {k}: {x}" for x in problems]
            try:
                res = json.loads((tdir / "result.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                res = None
            n_lines, bad = None, 0
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
                    state["transcript_unparsed_turns"].append(k)
            else:
                state["warnings"].append(f"turn {k}: 세션 기록 파일을 찾지 못함")
            line_ends.append(n_lines if n_lines is not None else (line_ends[-1] if line_ends else 0))
            # 결과의 modelUsage·total_cost_usd는 --resume 턴에서 세션 누적 → 앞 턴 대비 차분이 그 턴의 값(README "토큰·비용")
            cum_tok = tokens_of(res) if res else None
            cum_cost = res.get("total_cost_usd") if res else None
            spend, spend_warn = turn_spend(cum_tok, cum_cost, res.get("session_id") if res else None, spend_prev)
            state["warnings"] += [f"turn {k}: {x}" for x in spend_warn]
            spend_prev = {"session": (res or {}).get("session_id") or (spend_prev or {}).get("session"),
                          "tokens": cum_tok if cum_tok is not None else (spend_prev or {}).get("tokens"),
                          "cost": cum_cost if cum_cost is not None else (spend_prev or {}).get("cost")}
            tok, cost = spend["tokens"], spend["cost_usd"]
            if tok is None:
                tokens_known = False
            else:
                tokens_total += tok
            if cost is None:
                cost_known = False
            else:
                cost_total += cost
            head = git(["-c", f"safe.directory={work}", "rev-parse", "HEAD"], work, check=False).stdout.strip() or None
            cur_settings = (work / ".claude" / "settings.json")
            if not cur_settings.is_file() or sha256_bytes(cur_settings.read_bytes()) != settings_sha:
                tampered = True
            extra = sorted(p.name for p in (work / ".claude").iterdir()) if (work / ".claude").is_dir() else []
            turn = {"step": cfg["steps"][k], "index": k, "started_at": started, "ended_at": ended,
                    "started_iso": datetime.datetime.fromtimestamp(started, datetime.timezone.utc).isoformat(),
                    "wall_s": round(wall, 1), "exit_code": rc, "timed_out": timed_out, "container": cstate,
                    "result_parsed": res is not None,
                    "is_error": res.get("is_error") if res else None, "subtype": res.get("subtype") if res else None,
                    "result_session_id": res.get("session_id") if res else None,
                    "num_turns": res.get("num_turns") if res else None,
                    "tokens": tok, "cost_usd": cost, "tokens_cumulative": cum_tok, "cost_cumulative": cum_cost,
                    "spend_basis": spend["basis"], "usage_tokens": usage_tokens(res) if res else None,
                    "context_tokens_last": context_tokens_last(res) if res else None,
                    "usage": res.get("usage") if res else None, "model_usage": res.get("modelUsage") if res else None,
                    "git_head": head, "tracked_diff_sha256": tracked_diff_sha256(work),
                    "transcript_lines": n_lines, "transcript_bad_lines": bad, "claude_dir_files": extra,
                    "auto_memory_files": auto_memory_files(scratch / "projects"),
                    "reread": reread_hits(entries, line_ends[-2] if len(line_ends) > 1 else 0, line_ends[-1],
                                          session_id),
                    "message_sha256": sha256_bytes(msg.encode())}
            for f in extra:  # 작업 폴더 .claude/ 아래 settings.json 밖의 파일 = 자기 메모(파일 시스템으로도 확인)
                if f != "settings.json":
                    turn["reread"]["memo"].append({"line": None, "tool": None, "match": f".claude/{f}"})
            if res and res.get("session_id") not in (None, session_id):
                state["warnings"].append(f"turn {k}: 결과의 session_id가 다름 ({res.get('session_id')})")
            for f in turn["auto_memory_files"]:
                if f not in [x["path"] for x in state["auto_memory_files"]]:
                    state["auto_memory_files"].append({"turn": k, "path": f})
            state["turns"].append(turn)
            write_json(tdir / "meta.json", turn)
            state["boundaries"] = transcript.locate_boundaries(entries, line_ends, msgs)
            write_json(tdir / "boundaries.json", state["boundaries"])
            state["totals"] = {"tokens": tokens_total if tokens_known else None,
                               "cost_usd": round(cost_total, 4) if cost_known else None,
                               "wall_s": round(time.time() - t_start, 1)}
            failed = None
            if timed_out:
                failed = "time_cap" if wall_limited else "turn_timeout"
            elif cstate.get("oom_killed"):
                failed = "container_oom_killed"
            elif rc != 0 or res is None or res.get("is_error") or res.get("subtype") not in (None, "success"):
                failed = f"turn_failed(exit={rc}, subtype={res.get('subtype') if res else None})"
            elif tpath is None:
                failed = "transcript_missing"
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
            state["measurement_complete"] = True  # 모든 턴이 끝남(이 뒤의 운영 실패는 failure_reasons에 따로)
    except BaseException as e:  # 실행기 자체 오류·중단도 기록하고 다시 올린다
        state["status"], state["abort_reason"] = "aborted", f"runner_error: {e!r}"
        write_json(rdir / "run.json", state)
        raise
    finally:
        finalize(docker, cfg, a, state, rdir, root, names, created, entries, tampered, token)
    return EXIT.get(state["status"], 1)


def finalize(docker, cfg, a, state, rdir, root, names, created, entries, tampered, token):
    """끝(정상·중단·예외 모두): HOME 볼륨 사본 → 토큰 검사 → 기록 정리·보관 → Docker 리소스 정리 → 최종 상태."""
    work, scratch, c = root / "work", root / "scratch", cfg["container"]
    failures = []
    if ("volume", names["home"]) in created:
        created.append(("container", names["collect"]))
        p = docker.run("create", "--name", names["collect"], "--label", LABEL, "--network", "none",
                       "--mount", f"type=volume,src={names['home']},dst={c['home']}", cfg["image"], "true")
        if p.returncode == 0:
            cp = docker.run("cp", f"{names['collect']}:{c['home']}/.claude", str(scratch / "claude-home"), timeout=900)
            if cp.returncode != 0:
                state["warnings"].append(f"finalize: ~/.claude 사본 실패(rc={cp.returncode}) {cp.stderr.strip()[:200]}")
        else:
            state["warnings"].append(f"finalize: 수집 컨테이너 생성 실패 {p.stderr.strip()[:200]}")
    # 압축본(.tar.gz)은 바이트 검사로 볼 수 없으므로 검사하지 않는다. 대신 압축본은 **검사(와 가림)를 마친 폴더에서만** 만든다:
    # work.tar.gz ← work, claude-home.tar.gz ← scratch/claude-home. 검사와 압축 사이에 두 폴더에 쓰는 것은 없다.
    archives = [(work, rdir / "work.tar.gz", "work", ("build", ".gradle", ".kotlin"), "work_archive"),
                (scratch / "claude-home", rdir / "claude-home.tar.gz", "claude-home", (), "claude_home_archive")]
    try:
        # scratch 전체(~/.claude 사본 + 턴마다 복사한 세션 기록 projects/) — --keep-root면 남으므로 함께 검사
        hits = {"work": scrub(work, token), "scratch": scrub(scratch, token),
                "record": scrub(rdir, token, skip_suffixes=(".tar.gz",)),
                # git 객체 안의 토큰은 가릴 수 없다(객체 해시가 바뀜) → 찾으면 작업 폴더 압축본을 만들지 않는다(아래)
                "git_objects": git_objects_hits(work, token)}
        scanned = (work, scratch)
        state["secret_scan"] = {"hits": hits, "error": None, "archives_from_scanned": all(
            any(src == d or d in src.parents for d in scanned) for src, *_ in archives)}
    except Exception as e:  # 검사 자체 실패 = 범위를 모름 → 실패
        state["secret_scan"] = {"hits": None, "error": repr(e).replace(token, "<REDACTED-SECRET>"),
                                "archives_from_scanned": False}
        failures.append("secret_scan_error")
    if state["secret_scan"]["hits"] and any(state["secret_scan"]["hits"].values()):
        failures.append("secret_leak")
        (rdir / "SECRET-LEAK.txt").write_text(json.dumps(state["secret_scan"]["hits"], ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    hd = rdir / "hookdata"
    state["injections"] = read_jsonl(hd / "inject-log.jsonl")
    state["compact_log"] = [{k: v for k, v in r.items() if k != "compact_summary"} | {
        "summary_chars": len(r.get("compact_summary") or "")} for r in read_jsonl(hd / "compact-log.jsonl")]
    state["hook_errors"] = read_jsonl(hd / "hook-errors.jsonl")
    if state["condition"] == "outbox":
        state["outbox_boundary_injections"] = boundary_injections(state["compact_log"], state["injections"],
                                                                  len(state["boundaries"]))
    state["audit"] = audit(entries, cfg)
    state["unexpected_tools"] = unexpected_tools(entries, cfg)
    state["collection_data_reads"] = collection_data_reads(entries)
    reasons = invalid_reasons(state["condition"], state["boundaries"], state["audit"], tampered, state["injections"],
                              state["compact_log"], state["hook_errors"], bool(state["transcript_unparsed_turns"]),
                              state["collection_data_reads"], state["auto_memory_files"])
    state["invalid_reasons"], state["invalid"] = reasons, bool(reasons)
    if (state["secret_scan"].get("hits") or {}).get("git_objects"):
        archives = [a for a in archives if a[0] != work]
        state["secret_scan"]["work_archive_withheld"] = "git_objects"  # 보관하면 토큰이 든 git 객체가 그대로 남는다
    try:
        for src, dest, arcname, skip, key in archives:
            if src.is_dir():
                archive_dir(src, dest, arcname, skip=skip)
                state[key] = dest.name
    except Exception as e:
        state["warnings"].append(f"finalize: 보관 실패 {e!r}")
        failures.append("archive_failed")
    state["cleanup_errors"] += cleanup_stack(docker, created)
    if state["cleanup_errors"]:
        failures.append("docker_cleanup_failed")
    state["failure_reasons"] = failures
    if failures:
        state["status"] = "failed"
    state["ended_at"] = now_iso()
    write_json(rdir / "run.json", state)
    late = scrub(rdir, token, skip_suffixes=(".tar.gz",))  # run.json 등 마지막에 쓴 파일까지
    if late:
        state["secret_scan"]["late_hits"] = late
        state["status"] = "failed"
        state["failure_reasons"] = sorted(set(state["failure_reasons"]) | {"secret_leak"})
        write_json(rdir / "run.json", state)
    if not a.keep_root:
        shutil.rmtree(root, ignore_errors=True)
    print(json.dumps({k: state[k] for k in ("run_id", "status", "abort_reason", "failure_reasons", "invalid",
                                            "invalid_reasons")}, ensure_ascii=False))
    print(f"기록: {rdir}")


if __name__ == "__main__":
    sys.exit(main())
