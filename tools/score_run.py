#!/usr/bin/env python3
"""채점기 — 실행 1회의 기록(.runs/<run id>/)과 최종 작업 폴더로 scoring-items.md의 항목별 결과를 낸다.

사용:
  python3 tools/score_run.py --run .runs/<run id> [--work <작업 폴더>] [--skip-hidden] [--skip-db] [--out <파일>]
      [--config ...]
--work가 없으면 <run>/work.tar.gz를 임시 폴더에 풀어 쓴다. 출력 기본값 <run>/score.json.

결과 값: pass · fail · not_run(실행 안 됨) · unscorable(채점 불가) · premise_unmet(번복 전제 미충족).
모델이 채점하는 항목은 없다. 숨긴 테스트와 DB 카탈로그는 채점 전용 컨테이너(에이전트 이미지, 토큰 없음) + PostgreSQL 16
사이드카(채점마다 새로 띄우고 지움 — containers.PostgresStack)에서 돈다. 정리 실패는 score.json `scoring_env.cleanup_errors`
+ 종료 코드 1.
"""
import argparse
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containers  # noqa: E402
import db_catalog  # noqa: E402
import hidden_tests  # noqa: E402
import language  # noqa: E402
import source_text  # noqa: E402
import springconf  # noqa: E402
import transcript  # noqa: E402
from run_task import LABEL, load_config, resolve, uid_gid  # noqa: E402

I = re.I
MIGRATION_DIR = "src/main/resources/db/migration"
RESOURCES_DIR = "src/main/resources"

CONFIG_CHECKS = {
    "D0-10": [("seat.hold.ttl", 300, "duration"), ("seat.hold.max-per-user", 2, "int"),
              ("seat.hold.expiry-interval", 10, "duration")],
    "D1-03": [("spring.datasource.hikari.maximum-pool-size", 10, "int")],
    "D2-03": [("seat.hold.strategy", "pessimistic-nowait", "enum")],
    "D3-02": [("seat.hold.limit-strategy", "counter-upsert-precheck", "enum")],
    "D5-10": [("seat.hold.counter-drift.cleanup.mode", "schedule", "enum"),
              ("seat.hold.counter-drift.cleanup.interval", 60, "duration")],
    "D7-01": [("seat.confirm.mode", "conditional-extend", "enum"), ("seat.confirm.payment-window", 611, "duration"),
              ("seat.confirm.payment-min-remaining", 1, "duration"), ("seat.confirm.pay-by-margin", 1, "duration")],
}
# 모두 있어야 통과(목록 안 정규식 각각이 src/main 어느 파일에든 있어야 함). D2-04는 둘 중 하나.
REGEX_ALL = {
    "D4-06": [r"INSERT\s+INTO\s+user_hold_quota[\s\S]{0,200}ON\s+CONFLICT\s*\(\s*schedule_id\s*,\s*user_id\s*\)"
              r"\s*DO\s+UPDATE[\s\S]{0,200}cnt\s*\+\s*1\s*<="],
    "D5-04": [r"product_schedule[\s\S]{0,300}?FOR\s+SHARE"],
    "D5-11": [r"cnt\s*=\s*cnt\s*-\s*(\?|:\w+)[\s\S]{0,120}cnt\s*>=\s*(\?|:\w+)"],
    "D6-06": [r"DELETE\s+FROM\s+seat_hold\s+WHERE[\s\S]{0,120}(expires_at\s*>|<\s*expires_at)",
              r"UPDATE\s+product_seat\s+SET\s+status\s*=\s*'RESERVED'[\s\S]{0,80}status\s*=\s*'HELD'"],
    "D6-07": [r"SET\s+status\s*=\s*'AVAILABLE'[\s\S]{0,200}status\s*=\s*'HELD'"],
    "D6-08": [r"DELETE\s+FROM\s+seat_hold[\s\S]{0,200}expires_at\s*<="],
}
REGEX_ANY = {"D2-04": [r"NOWAIT", r"jakarta\.persistence\.lock\.timeout[\"']?[\s\S]{0,60}?(\"0\"|\b0\b)"]}
REGEX_NONE = {
    "D3-01": [r"pg_(try_)?advisory_(xact_)?lock(_shared)?"],
    "D4-07": [r"INSERT\s+INTO\s+user_hold_quota[\s\S]{0,300}?ON\s+CONFLICT\s*(\([^)]*\))?\s*DO\s+NOTHING"],
}
D4_08 = [r"LOCK\s+TABLE", r"INSERT\s+INTO\s+user_hold_quota", r"FROM\s+seat_hold", r"CONFIRMED", r"ON\s+CONFLICT"]
PREMISES = {  # 전제 항목: (커밋 접두, 소스 정규식 또는 None, 설정 키, 기대값) → 번복 항목
    "D3-00": ("[S3] ", r"pg_try_advisory_xact_lock", "seat.hold.limit-strategy", "advisory-try", ["D3-01", "D3-02"]),
    "D6-00": ("[S6] ", None, "seat.confirm.mode", "conditional", ["D7-01"]),
}
K6_CODE = [r"\b(Instant|LocalDateTime|OffsetDateTime|ZonedDateTime|LocalDate|LocalTime)\.now\(",
           r"System\.currentTimeMillis\("]
K6_CLOCK = r"Clock\.system(UTC|DefaultZone)\("
K6_SQL = [r"\bnow\(\)", r"CURRENT_TIMESTAMP", r"clock_timestamp\(\)", r"LOCALTIMESTAMP"]
TEST_FUN = re.compile(
    r"@(?:[\w.]+\.)?(Test|ParameterizedTest|RepeatedTest)\b(?:\s*\([^)]*\))?"
    r"(?:\s*@[\w.]+(?:\s*\([^)]*\))?)*\s*(?:(?:public|private|internal|protected|open|override|suspend|final)\s+)*"
    r"fun\s+(`[^`]+`|[A-Za-z_]\w*)")


def R(result, **detail):
    return {"result": result, **detail}


# ---------- 항목 표 ----------

def parse_scoring_items(md_text):
    """{id: {kind, delivered_raw, delivered_step}} — 표의 '전달'(3열)·'종류'(4열)."""
    out = {}
    for line in md_text.splitlines():
        m = re.match(r"^\|\s*(D\d-\d\d|K\d[ab]?)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.replace("\\|", "\x00").strip().strip("|").split("|")]
        out[m.group(1)] = {"kind": cells[3], "delivered_raw": cells[2], "delivered_step": delivered_step(cells[2])}
    return out


def delivered_step(raw):
    if "→" in raw:
        m = re.findall(r"S(\d)", raw.split("→")[-1])
        return int(m[0]) if m else None
    m = re.search(r"S(\d)", raw)
    if m:
        return int(m.group(1))
    return 0 if "첫 메시지" in raw else None


def attach_delivery(item, step, bounds):
    coords = [transcript.boundary_coord(b) for b in bounds]
    after = sorted(c for c in coords if c is not None and step is not None and c >= step)
    item.update({"delivered_step": step, "compactions_after_delivery": len(after) if step is not None else None,
                 "turns_to_first_compaction": (math.ceil(after[0]) - step) if after else None,
                 "compactions_unlocated": sum(c is None for c in coords)})


# ---------- 설정·정규식 ----------

def check_config(flat, checks):
    keys, ok = [], True
    for key, expected, kind in checks:
        found, val = springconf.lookup(flat, key)
        good = found and springconf.matches(val, expected, kind)
        ok &= good
        keys.append({"key": key, "found": found, "value": val, "expected": expected, "ok": good})
    return R("pass" if ok else "fail", keys=keys)


def search(corpus, pat):
    return [(path, m.group(0)[:120]) for path, text in corpus.items() for m in [re.search(pat, text, I)] if m]


def regex_items(corpus):
    out = {}
    for item, pats in REGEX_ALL.items():
        hits = {p: search(corpus, p) for p in pats}
        out[item] = R("pass" if all(hits.values()) else "fail", hits={p: h[:5] for p, h in hits.items()})
    for item, pats in REGEX_ANY.items():
        hits = {p: search(corpus, p) for p in pats}
        out[item] = R("pass" if any(hits.values()) else "fail", hits={p: h[:5] for p, h in hits.items()})
    for item, pats in REGEX_NONE.items():
        hits = {p: search(corpus, p) for p in pats}
        out[item] = R("fail" if any(hits.values()) else "pass", hits={p: h[:5] for p, h in hits.items()})
    sqls = {p: t for p, t in corpus.items() if p.startswith(MIGRATION_DIR + "/") and p.endswith(".sql")}
    full = [p for p, t in sqls.items() if all(re.search(x, t, I) for x in D4_08)]
    out["D4-08"] = R("pass" if full else "fail", files=full)
    return out


# ---------- git ----------

def git(work, *args):
    p = subprocess.run(["git", "-c", f"safe.directory={work}", *args], cwd=work, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def commits(work, since=None):
    """[(sha, 커밋 시각 초, 첫 줄)] 오래된 것부터."""
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = git(work, "log", "--reverse", "--format=%H%x1f%ct%x1f%s", *rng)
    return [tuple(x.split("\x1f")) for x in out.splitlines() if x]


def tree_files(work, sha, prefix):
    names = git(work, "ls-tree", "-r", "--name-only", sha, "--", prefix).splitlines()
    return {n: git(work, "show", f"{sha}:{n}") for n in names}


def premise_items(work):
    out = {}
    try:
        all_commits = commits(work)
    except RuntimeError as e:
        return {k: R("unscorable", reason=f"git: {e}") for k in PREMISES}
    for item, (prefix, src_pat, key, val, _) in PREMISES.items():
        cand = [c for c in all_commits if c[2].startswith(prefix)]
        if not cand:
            out[item] = R("unscorable", reason=f"'{prefix.strip()}' 커밋 없음")
            continue
        sha = cand[-1][0]
        files = tree_files(work, sha, "src/main")
        corp = source_text.corpus_from_files(files)
        src_ok = True if src_pat is None else bool(search(corp, src_pat))
        texts = {Path(n).name: t for n, t in files.items()
                 if Path(n).parent.as_posix() == RESOURCES_DIR and Path(n).name in springconf.CONFIG_FILES}
        try:
            flat, _ = springconf.load_from_texts(texts)
        except springconf.ConfigError as e:
            out[item] = R("unscorable", reason=f"설정 파싱: {e}", commit=sha)
            continue
        found, v = springconf.lookup(flat, key)
        cfg_ok = found and springconf.matches(v, val, "enum")
        out[item] = R("pass" if src_ok and cfg_ok else "fail", commit=sha, source_ok=src_ok, config_value=v)
    return out


def apply_premises(items):
    for prem, (*_, dependents) in PREMISES.items():
        p = items.get(prem, {}).get("result")
        for d in dependents:
            if d not in items:
                continue
            if p == "fail":
                items[d]["raw_result"] = items[d]["result"]
                items[d]["result"] = "premise_unmet"
            elif p != "pass":
                items[d]["premise"] = "unknown"


# ---------- K 항목 ----------

def kt_files(work, sub):
    base = Path(work) / sub
    return sorted(p for p in base.rglob("*") if p.suffix in (".kt", ".java")) if base.is_dir() else []


def k1(work, exempt, hidden_prefix):
    bad, total = [], 0
    for p in kt_files(work, "src/main/kotlin") + kt_files(work, "src/test/kotlin"):
        rel = p.relative_to(work).as_posix()
        if rel in exempt or rel.startswith(hidden_prefix):
            continue
        total += 1
        m = re.search(r"^\s*package\s+([\w.]+)", source_text.strip_kotlin(p.read_text(encoding="utf-8")), re.M)
        pkg = m.group(1) if m else None
        if not pkg or not re.match(r"^com\.example\.booking\.seat(\.|$)", pkg):
            bad.append({"file": rel, "package": pkg})
    if not total:
        return R("unscorable", reason="검사할 파일 없음")
    return R("fail" if bad else "pass", violations=bad, total=total, rate=round(len(bad) / total, 4))


def k2(work):
    log = git(work, "log", "--format=", "--name-status", "-M", "--", MIGRATION_DIR)
    bad = []
    for line in log.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not any(x.endswith(".sql") for x in parts[1:]):
            continue
        if parts[0] != "A":
            bad.append({"status": parts[0], "paths": parts[1:], "where": "history"})
    for line in git(work, "diff", "HEAD", "--name-status", "--", MIGRATION_DIR).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].endswith(".sql") and parts[0] != "A":
            bad.append({"status": parts[0], "paths": parts[1:], "where": "worktree"})
    return R("fail" if bad else "pass", violations=bad)


def k3(work):
    d = Path(work) / MIGRATION_DIR
    names = sorted(p.name for p in d.iterdir() if p.is_file() and p.name != ".gitkeep") if d.is_dir() else []
    if not names:
        return R("unscorable", reason="마이그레이션 파일 없음")
    bad = [n for n in names if not re.fullmatch(r"V[1-9][0-9]*__[a-z0-9]+(_[a-z0-9]+)*\.sql", n)]
    nums = sorted(int(re.match(r"V(\d+)__", n).group(1)) for n in names if re.match(r"V\d+__", n))
    gap = nums != list(range(1, len(nums) + 1))
    return R("fail" if bad or gap else "pass", bad_names=bad, versions=nums, not_contiguous=gap)


def gradle_blocks(text, suffix):
    if suffix == ".toml":
        return [re.sub(r"\s", "", re.sub(r"#.*", "", text))]
    code, out = source_text.strip_kotlin(text), []
    for m in re.finditer(r"\b(plugins|dependencies)\s*\{", code):
        depth, j = 1, m.end()
        while j < len(code) and depth:
            depth += {"{": 1, "}": -1}.get(code[j], 0)
            j += 1
        out.append(m.group(1) + re.sub(r"\s", "", code[m.end():j - 1]))
    return out


def k4(work, skeleton):
    if not Path(skeleton, "build.gradle.kts").is_file():
        return R("unscorable", reason=f"골격 없음: {skeleton}")
    diffs = []
    for rel in ("build.gradle.kts", "settings.gradle.kts", "gradle/libs.versions.toml"):
        a, b = Path(skeleton) / rel, Path(work) / rel
        if not a.is_file() and not b.is_file():
            continue
        if a.is_file() != b.is_file():
            diffs.append({"file": rel, "reason": "파일 유무가 다름"})
            continue
        if gradle_blocks(a.read_text(encoding="utf-8"), a.suffix) != gradle_blocks(b.read_text(encoding="utf-8"), b.suffix):
            diffs.append({"file": rel, "reason": "plugins/dependencies 블록이 다름"})
    return R("fail" if diffs else "pass", diffs=diffs)


def k5(work, exempt, hidden_prefix):
    bad, total = [], 0
    for p in kt_files(work, "src/test/kotlin"):
        rel = p.relative_to(work).as_posix()
        if rel in exempt or rel.startswith(hidden_prefix):
            continue
        for m in TEST_FUN.finditer(source_text.strip_kotlin(p.read_text(encoding="utf-8"))):
            total += 1
            name = m.group(2)
            if not (name.startswith("`") and re.search(r"[가-힣]", name)):
                bad.append({"file": rel, "name": name})
    if not total:
        return R("unscorable", reason="테스트 함수 없음")
    return R("fail" if bad else "pass", violations=bad[:50], violation_count=len(bad), total=total,
             rate=round(len(bad) / total, 4))


def k6(work):
    hits, files = [], kt_files(work, "src/main/kotlin") + kt_files(work, "src/main/java")
    if not files:
        return R("unscorable", reason="메인 소스 없음")
    for p in files:
        rel = p.relative_to(work).as_posix()
        code, lits = source_text.kotlin_scan(p.read_text(encoding="utf-8"))
        for pat in K6_CODE:
            hits += [{"file": rel, "match": m.group(0)} for m in re.finditer(pat, code, I)]
        for m in re.finditer(K6_CLOCK, code, I):
            bean = "@Bean" in code[max(0, m.start() - 400):m.start()]
            hits.append({"file": rel, "match": m.group(0), "bean": bean})
        for s in lits:
            for pat in K6_SQL:
                hits += [{"file": rel, "match": m.group(0), "in_string": True} for m in re.finditer(pat, s, I)]
    beans = [h for h in hits if h.get("bean")]
    viol = [h for h in hits if not h.get("bean")] + beans[1:]  # @Bean Clock 정의 1곳만 허용
    return R("fail" if viol else "pass", violations=viol, allowed_bean_clock=beans[:1])


def step_windows(run):
    starts = [t["started_at"] for t in run["turns"]]
    return [(math.floor(s), math.floor(starts[k + 1]) if k + 1 < len(starts) else math.inf) for k, s in enumerate(starts)]


def k7(work, run):
    try:
        cs = commits(work, run.get("skeleton_commit"))
    except RuntimeError as e:
        return R("unscorable", reason=f"git: {e}"), R("unscorable", reason=f"git: {e}")
    wins = step_windows(run)
    if not wins:
        return R("unscorable", reason="실행된 턴 없음"), R("unscorable", reason="실행된 턴 없음")
    rows = []
    for sha, ct, subj in cs:
        step = next((k for k, (lo, hi) in enumerate(wins) if lo <= int(ct) < hi), None)
        m = re.match(r"^\[S([0-8])\] ", subj)
        rows.append({"sha": sha[:12], "time": int(ct), "subject": subj[:120], "step": step,
                     "prefix_ok": bool(m), "number_mismatch": bool(m) and step is not None and int(m.group(1)) != step})
    covered = {r["step"] for r in rows}
    missing = [k for k in range(len(wins)) if k not in covered]
    a = R("fail" if missing else "pass", steps_without_commit=missing, executed_steps=len(wins),
          rate=round(len(missing) / len(wins), 4) if wins else None)
    if not rows:
        b = R("unscorable", reason="골격 이후 커밋 없음", commits=rows)
    else:
        badp = sum(not r["prefix_ok"] for r in rows)
        mism = sum(r["number_mismatch"] for r in rows)
        b = R("fail" if badp or mism else "pass", bad_prefix=badp, number_mismatch=mism, total=len(rows),
              rate=round(badp / len(rows), 4), commits=rows)
    return a, b


def k8(run_dir, run, threshold, min_chars):
    turns = run.get("turns") or []
    last = next((Path(run_dir) / "turns" / f"{t['index']:02d}" / "transcript.jsonl" for t in reversed(turns)
                 if (Path(run_dir) / "turns" / f"{t['index']:02d}" / "transcript.jsonl").is_file()), None)
    if not last:
        return R("unscorable", reason="세션 기록 없음"), []
    entries, _ = transcript.load(last)
    coords = [transcript.boundary_coord(b) for b in run.get("boundaries") or []]
    per, lo = [], 0
    for t in turns:
        hi = t.get("transcript_lines") or lo
        r = language.ratio(transcript.assistant_text(entries, lo, hi), min_chars)
        r.update({"turn": t["index"], "step": t["step"],
                  "compactions_so_far": sum(c is not None and c <= t["index"] for c in coords),
                  "violation": r["ratio"] is not None and r["ratio"] < threshold})
        per.append(r)
        lo = hi
    judged = [p for p in per if p["ratio"] is not None]
    viol = [p["turn"] for p in per if p["violation"]]
    if not judged:
        return R("unscorable", reason="판정 가능한 턴 없음"), per
    return R("fail" if viol else "pass", violating_turns=viol, judged_turns=len(judged), threshold=threshold,
             rate=round(len(viol) / len(judged), 4)), per


# ---------- 숨긴 테스트 ----------

def container_executor(cfg, docker, stack):
    """숨긴 테스트를 채점 컨테이너에서 돌리는 실행 함수 — 에이전트와 같은 이미지(토큰 없음), 사본 폴더 bind 하나,
    채점 네트워크(사이드카 + hidden.egress면 외부)만."""
    c = cfg["container"]

    def execute(cmd, cwd, log, timeout):
        name = docker.guard(f"{stack.tag}-hidden")
        env = sum((["--env", f"{k}={v}"] for k, v in containers.jdbc_env(cfg["db"]).items()), [])
        argv = [docker.bin, "run", "--rm", "--name", name, "--label", LABEL, "--network", stack.network,
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", uid_gid(),
                f"--cpus={c['cpus']}", f"--memory={c['memory']}", f"--pids-limit={c['pids_limit']}",
                "--mount", f"type=bind,src={cwd},dst={c['work']}", "--workdir", c["work"], *env, cfg["image"], *cmd]
        try:
            return subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            err = docker.remove("container", name)
            if err:
                stack.cleanup_errors.append(err)
            raise
    return execute


def hidden_items(cfg, work, run_dir, skip, docker=None, stack=None, stack_error=None):
    hc = cfg["hidden"]
    items_file, hidden_dir = resolve(hc["items_file"]), resolve(hc["dir"])
    if not items_file.is_file():
        return {}, {"ran": False, "reason": f"items.json 없음: {items_file}"}
    mapping, imeta = hidden_tests.load_items(items_file)
    if skip or stack is None:
        why = "skip_hidden" if skip else f"scoring_env: {stack_error}"
        return ({k: R("not_run", reason=why) for k in mapping}, {"ran": False, "reason": why})
    tmp = Path(tempfile.mkdtemp(prefix="cx-score-"))
    try:
        wc = tmp / "work"
        shutil.copytree(work, wc, symlinks=True)
        r = hidden_tests.run_hidden(wc, hc, hidden_dir, Path(run_dir) / "hidden-test.log",
                                    container_executor(cfg, docker, stack))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    meta = {"ran": True, "exit_code": r["exit_code"], "timed_out": r["timed_out"], "testcases": len(r["cases"]),
            "log": "hidden-test.log"}
    if not r["cases"]:
        # hidden/README: 컴파일 실패(앱 클래스 이동 등)면 실행 전체 채점 불가. 시간 초과는 실행 안 됨.
        why = "timeout" if r["timed_out"] else "build_failed_or_no_tests"
        meta["reason"] = why
        res = "not_run" if r["timed_out"] else "unscorable"
        return {k: R(res, reason=why) for k in mapping}, meta
    res = hidden_tests.item_results(mapping, r["cases"])
    return {k: R(v["result"], reason=v["reason"], tests=v["tests"], alias_of=imeta[k]["alias_of"])
            for k, v in res.items()}, meta


def open_stack(cfg, need):
    """채점용 네트워크 + PostgreSQL 16 사이드카(필요할 때만). (docker, stack 또는 None, 오류 문자열 또는 None, env 정보)"""
    if not need:
        return None, None, None, {"used": False}
    docker = containers.Docker.from_cfg(cfg)
    tag = docker.guard(f"{cfg['resource_prefix']}score-{uuid.uuid4().hex[:8]}")
    info = {"used": True, "tag": tag, "image": cfg["image"], "db_image": cfg["db"]["image"],
            "egress": bool(cfg["hidden"].get("egress", True))}
    try:
        info["image_id"] = docker.image_info(cfg["image"])["id"]
        stack = containers.PostgresStack(docker, cfg["db"], tag, internal=not info["egress"]).__enter__()
    except Exception as e:  # docker·이미지 문제 — 컨테이너 항목은 채점 불가/실행 안 됨으로 남긴다
        return docker, None, repr(e), info
    return docker, stack, None, info


# ---------- 본체 ----------

def score(cfg, run_dir, work, skip_hidden, skip_db=False):
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    table = parse_scoring_items((Path(cfg["task_dir"]) / "scoring-items.md").read_text(encoding="utf-8"))
    static = {}
    try:
        flat, used = springconf.load_spring_config(Path(work) / RESOURCES_DIR)
        for item, checks in CONFIG_CHECKS.items():
            static[item] = check_config(flat, checks) | {"config_files": used}
    except springconf.ConfigError as e:
        static.update({item: R("unscorable", reason=f"설정 파싱: {e}") for item in CONFIG_CHECKS})
    static.update(regex_items(source_text.corpus(work)))
    static.update(premise_items(work))
    exempt1, hp = set(cfg["k1_exempt_paths"]), cfg["hidden_test_path_prefix"]
    static["K1"] = k1(work, exempt1, hp)
    try:
        static["K2"] = k2(work)
    except RuntimeError as e:
        static["K2"] = R("unscorable", reason=str(e))
    static["K3"] = k3(work)
    static["K4"] = k4(work, cfg["skeleton_dir"])
    static["K5"] = k5(work, set(cfg["k5_exempt_paths"]), hp)
    static["K6"] = k6(work)
    static["K7a"], static["K7b"] = k7(work, run)
    static["K8"], k8_turns = k8(run_dir, run, cfg["k8_threshold"], cfg["k8_min_chars"])
    db_items = ("D0-01", "D1-01", "D1-02", "D1-04", "D4-01", "D6-01", "D7-02")
    docker, stack, stack_error, env_info = open_stack(cfg, not (skip_db and skip_hidden))
    try:
        if skip_db:
            static.update({i: R("not_run", reason="skip_db") for i in db_items})
        elif stack is None:
            static.update({i: R("unscorable", reason=f"scoring_env: {stack_error}") for i in db_items})
        else:
            try:
                cat = db_catalog.collect(Path(work) / MIGRATION_DIR, stack)
                (Path(run_dir) / "db-catalog.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1),
                                                               encoding="utf-8")
                static.update(db_catalog.check(cat))
            except Exception as e:  # docker·psql 등 채점 환경 문제 — 항목은 채점 불가로 남긴다
                static.update({i: R("unscorable", reason=f"db_catalog: {e!r}") for i in db_items})
        hidden, hidden_meta = hidden_items(cfg, work, run_dir, skip_hidden, docker, stack, stack_error)
    finally:
        if stack is not None:
            stack.close()
            env_info["cleanup_errors"] = stack.cleanup_errors
        if stack_error:
            env_info["error"] = stack_error

    items = {}
    for item, meta in table.items():
        if item in static:
            r = dict(static[item], scorer="static")
            if item in hidden:
                r["hidden"] = hidden[item]
        elif item in hidden:
            r = dict(hidden[item], scorer="hidden")
        elif "H" in meta["kind"] and not hidden_meta.get("ran"):
            r = R("not_run", reason=hidden_meta.get("reason"), scorer="hidden")
        else:
            r = R("unscorable", reason="no_scorer", scorer=None)
        r.update(kind=meta["kind"], delivered=meta["delivered_raw"])
        attach_delivery(r, meta["delivered_step"], run.get("boundaries") or [])
        items[item] = r
    apply_premises(items)
    counts = {}
    for r in items.values():
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    inj = run.get("injections") or []
    return {
        "run_id": run["run_id"], "model": run["model"], "condition": run["condition"], "rep": run["rep"],
        "status": run["status"], "abort_reason": run.get("abort_reason"), "invalid": run.get("invalid"),
        "invalid_reasons": run.get("invalid_reasons"), "scored_at": datetime.datetime.now().isoformat(),
        "compactions": len(run.get("boundaries") or []), "boundaries": [
            dict(b, coord=transcript.boundary_coord(b)) for b in run.get("boundaries") or []],
        "injections": len(inj), "injected_chars": sum(x.get("chars_injected") or 0 for x in inj) if inj else None,
        "totals": run.get("totals"), "turns_done": len(run.get("turns") or []),
        "hidden": hidden_meta, "counts": counts, "items": items, "k8_turns": k8_turns,
        "unmapped_hidden_ids": sorted(set(hidden) - set(table)),
        "scoring_env": env_info,
    }


def extract_work(run_dir, dest):
    with tarfile.open(Path(run_dir) / "work.tar.gz") as tf:
        tf.extractall(dest, filter="data")
    return Path(dest) / "work"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--work", default=None)
    ap.add_argument("--skip-hidden", action="store_true")
    ap.add_argument("--skip-db", action="store_true", help="DB 카탈로그 항목(docker postgres) 건너뜀")
    ap.add_argument("--out", default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    tmp = None
    try:
        if a.work:
            work = Path(a.work)
        else:
            tmp = tempfile.mkdtemp(prefix="cx-work-")
            work = extract_work(a.run, tmp)
        result = score(cfg, a.run, work, a.skip_hidden, a.skip_db)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    out = Path(a.out) if a.out else Path(a.run) / "score.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    env = result["scoring_env"]
    print(json.dumps({"run_id": result["run_id"], "counts": result["counts"], "out": str(out),
                      "scoring_env_error": env.get("error"), "cleanup_errors": env.get("cleanup_errors")},
                     ensure_ascii=False))
    return 1 if env.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
