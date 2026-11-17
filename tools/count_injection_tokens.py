#!/usr/bin/env python3
"""아웃박스 주입문의 토큰 수를 사후에 잰다(유료 — 모델 호출). 결과: <run>/injection-tokens.json → score_run이 읽는다.

사용:
  python3 tools/count_injection_tokens.py --run .runs/<run id> --workdir <저장소 밖 빈 폴더> \
      [--model <기본: run.json의 모델>] [--repeat 3] [--claude-bin claude]

방법(prompt-language-experiment/tools/measure_tokens.py와 같음): `claude -p`에 주입문 + 고정 꼬리 한 줄을 넣어 부르고
첫 요청 usage의 input + cache_creation + cache_read 토큰을 더한다. 꼬리만 넣은 호출(기준값)의 중앙값을 빼면 주입문의
토큰 수가 남는다. 도구·MCP·사용자 설정은 끈다(입력 고정).
주입문은 기록에서 다시 만든다: 실행 기록의 hookdata/outbox/<session>.jsonl 중 inject-log의 seqs 행을 inject.build_context로
같은 설정(N·L·rule)으로 이어 붙이고, inject-log의 context_sha256과 같아야 잰다(다르면 중단 — 엉뚱한 글을 재지 않는다).
재는 것은 additionalContext 본문뿐이다(CLI가 맥락에 넣을 때 두르는 틀은 빠진다).
"""
import argparse
import datetime
import hashlib
import json
import statistics
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS / "outbox"))
import inject  # noqa: E402

ROOT = TOOLS.parent
TAIL = "\n\nReply with exactly: ok\n"


def read_jsonl(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def contexts(run_dir):
    """[(inject-log 행, 주입문)] — 기록에서 다시 만든 주입문. 해시가 다르면 ValueError."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("condition") != "outbox":
        raise ValueError(f"아웃박스 조건 실행이 아님: {run.get('condition')}")
    rule = run["outbox_config"]["rule"]
    rows = read_jsonl(run_dir / "hookdata" / "outbox" / f"{run['session_id']}.jsonl")
    out = []
    for i, e in enumerate(run.get("injections") or []):
        if hashlib.sha256(rule.encode()).hexdigest() != e.get("rule_sha256"):
            raise ValueError(f"주입 {i}: rule 해시가 run.json outbox_config와 다름")
        picked = [r for r in rows if r["seq"] in set(e["seqs"])]
        text, seqs, _ = inject.build_context(picked, e["recent_messages"], e["max_chars_per_message"], rule)
        if seqs != e["seqs"] or hashlib.sha256(text.encode()).hexdigest() != e.get("context_sha256"):
            raise ValueError(f"주입 {i}: 다시 만든 주입문의 해시가 inject-log와 다름(또는 해시 기록 없음)")
        out.append((e, text))
    return out


def call(claude_bin, model, text, workdir, tries=3):
    """첫 요청의 입력 토큰 수(input + cache_creation + cache_read)."""
    last = ""
    for _ in range(tries):
        done = subprocess.run(
            [claude_bin, "-p", "--model", model, "--output-format", "json", "--max-turns", "1", "--tools", "",
             "--strict-mcp-config", "--mcp-config", str(Path(workdir) / "empty-mcp.json"), "--setting-sources", "project"],
            input=text, capture_output=True, text=True, cwd=workdir, timeout=300)
        try:
            data = json.loads(done.stdout)
            first = (data["usage"].get("iterations") or [data["usage"]])[0]
            served = sorted(data.get("modelUsage", {}))
            if not any(m.startswith(model) for m in served):
                raise RuntimeError(f"요청 {model}, 응답 {served}")
            return (first["input_tokens"] + first.get("cache_creation_input_tokens", 0)
                    + first.get("cache_read_input_tokens", 0))
        except (ValueError, KeyError, IndexError) as e:
            last = f"exit {done.returncode}: {type(e).__name__} {e}; stderr {done.stderr[-300:]!r}"
    raise RuntimeError(f"claude 호출 {tries}회 실패: {last}")


def measure(run_dir, model, repeat, workdir, claude_bin):
    run_dir, workdir = Path(run_dir), Path(workdir).resolve()
    if ROOT in workdir.parents or workdir == ROOT:
        raise SystemExit("--workdir는 저장소 밖이어야 한다(프로젝트 설정이 입력에 섞이지 않게)")
    items = contexts(run_dir)  # 해시가 맞지 않으면 호출 전에 중단
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    out_path = run_dir / "injection-tokens.json"
    result = {"model": model, "repeat": repeat, "method": "claude -p 입력 토큰(주입문+꼬리) − 기준값(꼬리만) 중앙값",
              "baseline_raw": [], "baseline_median": None, "injections": [], "total_tokens": None, "complete": False,
              "measured_at": None}

    def save():
        result["measured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    def baseline():  # 기준값은 앞과 주입문 사이사이에 잰다(시간에 따른 흔들림)
        result["baseline_raw"].append(call(claude_bin, model, TAIL.lstrip("\n"), workdir))
        result["baseline_median"] = statistics.median(result["baseline_raw"])

    baseline()
    for i, (e, text) in enumerate(items):
        raw = [call(claude_bin, model, text + TAIL, workdir) for _ in range(repeat)]
        result["injections"].append({"index": i, "seqs": e["seqs"], "chars": len(text),
                                     "context_sha256": e["context_sha256"], "raw": raw})
        baseline()
        save()
    for x in result["injections"]:
        x["tokens"] = statistics.median(x["raw"]) - result["baseline_median"]
    result["total_tokens"] = sum(x["tokens"] for x in result["injections"])
    result["complete"] = True
    save()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--model", default=None, help="기본: run.json의 모델(토큰 수는 모델의 토크나이저에 따른다)")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--claude-bin", default="claude")
    a = ap.parse_args(argv)
    model = a.model or json.loads((Path(a.run) / "run.json").read_text(encoding="utf-8"))["model"]
    r = measure(a.run, model, a.repeat, a.workdir, a.claude_bin)
    print(json.dumps({"total_tokens": r["total_tokens"], "injections": len(r["injections"]),
                      "baseline_median": r["baseline_median"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
