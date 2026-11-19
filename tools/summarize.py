#!/usr/bin/env python3
"""집계 — 실행 기록(run.json 기준으로 열거)과 채점(score.json)을 조건·모델별 표로.

사용: python3 tools/summarize.py [실행 폴더 | run.json | score.json ...] [--runs-dir .runs] [--json out.json]
인자가 없으면 <runs-dir>/*/run.json 전부. score.json이 없는 실행은 "미채점"으로 센다(빠뜨리지 않는다).

- 집계 대상 = 측정 완료(run.json measurement_complete — 모든 턴이 끝남) · 유효(invalid 아님) · 입력 일치 · 채점됨.
  채점됨 = score.json이 있고 그 신원(run_id·model·condition·채점한 run.json의 sha256)이 같은 폴더 run.json과 같음 — 다르면
  "채점 불일치"로 빠진 실행 목록에 이유와 함께 내고(표준 오류에도 경고) 섞지 않는다.
  운영 문제(정리·보관 실패, 토큰 검출 — run.json failure_reasons)는 측정을 무효로 하지 않고 따로 센다.
- 입력 일치: 모든 실행에서 같아야 할 것(COMMON_INPUTS)과 같은 조건 안에서 같아야 할 것(CONDITION_INPUTS)을 대조한다.
  가장 많은 값과 다르면(최다 값이 둘 이상이면 그 값을 가진 실행 모두) "입력 불일치"로 따로 내고 섞지 않는다.
- 위반율 = fail / (pass + fail). not_run·unscorable·premise_unmet는 분모에서 빼고 따로 센다.
  별칭 항목(alias_of — 같은 테스트)은 세지 않는다. 버릴 후보(●, drop_candidate)는 주 지표에서 빼고 따로 낸다.
- 주 측정값(명세 §2): 컴팩트 실행(compact·outbox·standing)마다 "전달 뒤 최종 시점까지 위치가 확정된 컴팩트가 한 번 이상 있었던
  항목" 집합을 정하고, 같은 모델 대조군 실행 하나하나와 그 집합 중 **양쪽 모두 판정된 항목만으로** 위반율 차이를 낸다(한쪽이라도
  판정 불가면 그 비교에서 양쪽 모두 뺌 — 비교마다 비교 항목 수·뺀 수를 낸다). 비교 항목이 없는 대조군은 대조군 수에 넣지 않는다.
  위치 불명 경계(프롬프트 앞/뒤를 확정하지 못함)만 있는 항목은 집합을 정하는 데 쓰지 않고 "불명"으로 따로 센다.
"""
import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from run_task import run_spend  # noqa: E402  옛 형식(턴 값 = 세션 누적) 기록의 토큰·비용을 차분으로 다시 계산
COMPACT_CONDITIONS = ("compact", "outbox", "standing")
COMMON_INPUTS = ("messages_sha256_all", "image_id", "skeleton_commit", "cli_version", "limits", "container_limits",
                 "container_env_common", "auto_memory")
CONDITION_INPUTS = ("settings_sha256", "compact_window_env", "outbox_config_sha256", "standing_config_sha256")
REREAD_KINDS = ("session_log", "outbox_data", "root_search")
K_ITEMS = ("K1", "K2", "K3", "K4", "K5", "K6", "K7a", "K7b", "K8")


def rate(f, p):
    return None if f + p == 0 else f / (f + p)


def fmt(x, pct=False, none="—"):
    if x is None:
        return none
    return f"{x * 100:.1f}%" if pct else (f"{x:.1f}" if isinstance(x, float) else str(x))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def span(xs):
    xs = [x for x in xs if x is not None]
    return (min(xs), max(xs)) if xs else None


# ---------- 입력 ----------

def load(paths):
    """[{dir, run, run_sha256, score 또는 None}] — 경로는 실행 폴더·run.json·score.json 아무거나."""
    out = []
    for p in paths:
        d = Path(p).parent if Path(p).is_file() else Path(p)
        if not (d / "run.json").is_file():
            raise SystemExit(f"run.json 없음: {d}")
        s = d / "score.json"
        raw = (d / "run.json").read_bytes()
        out.append({"dir": str(d), "run": json.loads(raw.decode("utf-8")), "run_sha256": hashlib.sha256(raw).hexdigest(),
                    "score": json.loads(s.read_text(encoding="utf-8")) if s.is_file() else None})
    return out


def score_mismatch(rec):
    """score.json이 같은 폴더 run.json의 채점인지 — 다른 키 목록(빈 목록 = 일치). 채점기가 남긴 신원(run_id·model·condition과
    채점한 run.json의 해시)을 대조한다. 해시가 없는 옛 채점도 불일치(다시 채점)."""
    s, run = rec["score"], rec["run"]
    if not s:
        return []
    bad = [k for k in ("run_id", "model", "condition") if s.get(k) != run.get(k)]
    return bad + (["run_sha256"] if s.get("run_sha256") != rec.get("run_sha256") else [])


def input_value(run, key):
    if key == "container_env_common":
        v = {k: x for k, x in (run.get("container_env") or {}).items() if k != "CLAUDE_CODE_AUTO_COMPACT_WINDOW"}
    else:
        v = run.get(key)
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def input_mismatches(recs):
    """{run_id: [어긋난 키]} — 모델마다 따로 대조한다(비교는 같은 모델 안에서만 하고, 모델끼리는 나란히 낼 뿐이다.
    예: Sonnet 파일럿과 Haiku 본 실행의 실행 상한(limits)이 달라도 서로를 입력 불일치로 만들지 않는다)."""
    bad = {}

    def check(group, key):
        vals = [input_value(r["run"], key) for r in group]
        top = Counter(vals).most_common(2)
        ref = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else None
        for r, v in zip(group, vals):
            if v != ref:
                bad.setdefault(r["run"]["run_id"], []).append(key)

    for model in sorted({r["run"]["model"] for r in recs}):
        mine = [r for r in recs if r["run"]["model"] == model]
        for key in COMMON_INPUTS:
            check(mine, key)
        for cond in sorted({r["run"]["condition"] for r in mine}):
            for key in CONDITION_INPUTS:
                check([r for r in mine if r["run"]["condition"] == cond], key)
    return bad


# ---------- 항목 ----------

def counted(r):
    return not r.get("alias_of")


def main_item(r):
    return counted(r) and not r.get("drop_candidate")


def bucket(r):
    """전달 뒤 컴팩트: after(위치 확정 경계 ≥1) · none(경계 없음) · unknown(확정 경계 0, 위치 불명 ≥1 또는 전달 단계 모름)."""
    after = r.get("compactions_after_delivery")
    if after is None:
        return "unknown"
    if after >= 1:
        return "after"
    return "unknown" if r.get("compactions_unlocated") else "none"


def item_rate(score, items):
    f = p = 0
    for i in items:
        res = score["items"].get(i, {}).get("result")
        f, p = f + (res == "fail"), p + (res == "pass")
    return rate(f, p), f + p


def reread_flags(run):
    turns = run.get("turns") or []
    reread = any((t.get("reread") or {}).get(k) for t in turns for k in REREAD_KINDS)
    memo = any((t.get("reread") or {}).get("memo") for t in turns)
    return reread, memo


# ---------- 집계 ----------

def summarize(recs):
    bad_inputs = input_mismatches(recs)
    for r in recs:
        run, s = r["run"], r["score"]
        r["mismatch"] = bad_inputs.get(run["run_id"], [])
        r["score_mismatch"] = score_mismatch(r)
        r["included"] = (bool(s) and not r["score_mismatch"] and run["measurement_complete"] and not run.get("invalid")
                         and not r["mismatch"])
    groups = {}
    for r in recs:
        groups.setdefault((r["run"]["model"], r["run"]["condition"]), []).append(r)
    rows, per_item = [], {}
    for (model, cond), rs in sorted(groups.items()):
        ok = [r for r in rs if r["included"]]
        f = p = fd = pd = 0
        other, run_rates = {}, []
        for r in ok:
            for item, it in r["score"]["items"].items():
                if not counted(it):
                    continue
                res = it["result"]
                cell = per_item.setdefault(item, {"drop_candidate": bool(it.get("drop_candidate")), "cells": {}})
                b = cell["cells"].setdefault((model, cond), {"after": [0, 0], "none": [0, 0], "unknown": [0, 0]})
                if res in ("pass", "fail"):
                    b[bucket(it)][0] += res == "fail"
                    b[bucket(it)][1] += 1
                    if it.get("drop_candidate"):
                        fd, pd = fd + (res == "fail"), pd + (res == "pass")
                    else:
                        f, p = f + (res == "fail"), p + (res == "pass")
                else:
                    other[res] = other.get(res, 0) + 1
            run_rates.append(item_rate(r["score"], [i for i, it in r["score"]["items"].items() if main_item(it)])[0])
        flags = [reread_flags(r["run"]) for r in rs]
        rows.append({
            "model": model, "condition": cond, "runs": len(rs),
            "measurement_complete": sum(bool(r["run"]["measurement_complete"]) for r in rs),
            "aborted": sum(not r["run"]["measurement_complete"] for r in rs),
            "unscored": sum(r["score"] is None for r in rs),
            "score_mismatch": sum(bool(r["score_mismatch"]) for r in rs),
            "invalid": sum(bool(r["run"].get("invalid")) for r in rs),
            "input_mismatch": sum(bool(r["mismatch"]) for r in rs),
            "included": len(ok),
            "ops_problems": sum(bool(r["run"].get("failure_reasons")) for r in rs),
            "secret_leak": sum("secret_leak" in (r["run"].get("failure_reasons") or []) for r in rs),
            "history_rewritten": sum(bool(((r["score"] or {}).get("history") or {}).get("rewritten_turns"))
                                     for r in rs if not r["score_mismatch"]),
            "reread_runs": sum(x[0] for x in flags), "memo_runs": sum(x[1] for x in flags),
            "violation_rate": rate(f, p), "judged": f + p,
            "drop_candidate_rate": rate(fd, pd), "drop_candidate_judged": fd + pd,
            "run_violation_rates": run_rates, "not_judged": other,
            "mean_compactions": mean([r["score"].get("compactions") for r in ok]),
            "unlocated_boundaries": sum(sum(b.get("coord") is None for b in r["score"].get("boundaries") or [])
                                        for r in ok),
            "prompt_adjacent_boundaries": sum(sum(b.get("mid_turn_phase") == "before_first_output"
                                                  for b in r["score"].get("boundaries") or []) for r in ok),
            "mean_injected_chars": mean([r["score"].get("injected_chars") for r in ok]),
            "mean_injected_tokens": mean([r["score"].get("injected_tokens") for r in ok]),
            "mean_tokens": mean([run_spend(r["run"])[0]["tokens"] for r in ok]),
            "mean_cost_usd": mean([run_spend(r["run"])[0]["cost_usd"] for r in ok]),
            "spend_recomputed": sum(run_spend(r["run"])[1] for r in ok),
        })
    pairs, primary = paired(recs)
    return {"rows": rows, "per_item": per_item, "pairs": pairs, "primary": primary,
            "runs": [run_record(r) for r in sorted(recs, key=lambda r: (r["run"]["model"], r["run"]["condition"],
                                                                      r["run"].get("rep") or 0, r["run"]["run_id"]))],
            "k_items": k_items(recs),
            "excluded": [{"run_id": r["run"]["run_id"], "model": r["run"]["model"], "unscored": r["score"] is None,
                          "score_mismatch": r["score_mismatch"],
                          "measurement_complete": r["run"]["measurement_complete"],
                          "invalid_reasons": r["run"].get("invalid_reasons") or [], "input_mismatch": r["mismatch"],
                          "failure_reasons": r["run"].get("failure_reasons") or []}
                         for r in recs if not r["included"] or r["run"].get("failure_reasons")]}


def run_record(r):
    """실행별 표 한 줄 — 컴팩트·주입 수·토큰과 K8(전체·턴 단위·첫 컴팩트 앞/뒤 본문 합). 채점이 이 실행의 것이 아니면 채점 값은 None."""
    run = r["run"]
    s = r["score"] if r["score"] and not r["score_mismatch"] else None
    k8 = ((s or {}).get("items") or {}).get("K8") or {}
    spend, recomputed = run_spend(run)
    rec = {"model": run["model"], "condition": run["condition"], "rep": run.get("rep"), "run_id": run["run_id"],
            "included": r["included"], "compactions": len(run.get("boundaries") or []),
            "injections": len(run.get("injections") or []),
            "injected_tokens": (s or {}).get("injected_tokens"), "tokens": spend["tokens"],
            "cost_usd": spend["cost_usd"], "spend_recomputed": recomputed,
            "k8": k8.get("result"), "k8_violating_turns": k8.get("violating_turns"),
            "k8_turn_level": (k8.get("turn_level") or {}).get("result"),
            "k8_turn_level_violating_turns": (k8.get("turn_level") or {}).get("violating_turns"),
            "k8_by_first_compaction": k8.get("by_first_compaction")}
    if run["condition"] == "standing":  # 고정 지시 훅 호출 수(run.json standing_counts) — 다른 조건의 기록에는 키가 없다
        rec["standing_counts"] = run.get("standing_counts")
    return rec


def k_items(recs):
    """제약별(K1~K8) 결과 — {모델: {조건: {항목: [(반복, 결과)]}}} (집계 대상 실행만, 반복 순)."""
    out = {}
    for r in sorted((r for r in recs if r["included"]), key=lambda r: (r["run"].get("rep") or 0, r["run"]["run_id"])):
        cell = out.setdefault(r["run"]["model"], {}).setdefault(r["run"]["condition"], {})
        for i in K_ITEMS:
            cell.setdefault(i, []).append((r["run"].get("rep"), (r["score"]["items"].get(i) or {}).get("result")))
    return out


def judged_items(score, items):
    return [i for i in items if score["items"].get(i, {}).get("result") in ("pass", "fail")]


def paired(recs):
    """주 측정값 — 컴팩트 실행마다, 같은 모델 대조군 하나하나와 **같은 항목끼리** 비교한다.

    비교 하나(컴팩트 실행 r, 대조군 c) = 전달 뒤 컴팩트 항목 집합 중 r과 c 양쪽에서 모두 판정된(pass/fail) 항목만 쓴다 —
    어느 한쪽이라도 판정 불가(unscorable·not_run·premise_unmet·없음)면 그 비교에서 양쪽 모두 뺀다(비교 항목 수·뺀 수를 남김).
    같은 항목에서 r의 위반율 − c의 위반율이 그 비교의 차이. 비교 항목이 0개인 대조군은 대조군 수에 넣지 않는다.
    실행의 차이 = 비교들의 차이 평균(r 위반율·대조군 위반율도 비교들의 평균)."""
    ok = [r for r in recs if r["included"]]
    pairs = []
    for r in ok:
        run = r["run"]
        if run["condition"] not in COMPACT_CONDITIONS:
            continue
        items = r["score"]["items"]
        target = sorted(i for i, it in items.items() if main_item(it) and bucket(it) == "after")
        unknown = sum(main_item(it) and bucket(it) == "unknown" for it in items.values())
        own = judged_items(r["score"], target)
        comps = []
        for c in ok:
            if c["run"]["condition"] != "control" or c["run"]["model"] != run["model"]:
                continue
            both = judged_items(c["score"], own)
            comps.append({"control": c["run"]["run_id"], "compared": len(both), "left_out": len(target) - len(both),
                          "rate": item_rate(r["score"], both)[0], "control_rate": item_rate(c["score"], both)[0]})
        used = [x for x in comps if x["compared"]]
        for x in used:
            x["diff"] = x["rate"] - x["control_rate"]
        crates = [x["control_rate"] for x in used]
        pairs.append({"model": run["model"], "condition": run["condition"], "run_id": run["run_id"],
                      "items": len(target), "unknown_items": unknown, "judged": len(own),
                      "own_rate": item_rate(r["score"], own)[0],
                      "rate": mean([x["rate"] for x in used]),
                      "controls": len(used), "controls_without_items": len(comps) - len(used),
                      "compared_items": [x["compared"] for x in used], "left_out_items": [x["left_out"] for x in used],
                      "comparisons": comps,
                      "control_rate_mean": mean(crates), "control_rate_span": span(crates),
                      "diff": mean([x["diff"] for x in used])})
    primary = []
    for key in sorted({(x["model"], x["condition"]) for x in pairs}):
        xs = [x for x in pairs if (x["model"], x["condition"]) == key]
        diffs = [x["diff"] for x in xs]
        primary.append({"model": key[0], "condition": key[1], "runs": len(xs),
                        "runs_with_diff": sum(d is not None for d in diffs),
                        "rate_mean": mean([x["rate"] for x in xs]),
                        "control_rate_mean": mean([x["control_rate_mean"] for x in xs]),
                        "diff_mean": mean(diffs), "diff_span": span(diffs)})
    return pairs, primary


# ---------- 출력 ----------

def pct_span(s):
    return "—" if s is None else f"{s[0] * 100:.1f}~{s[1] * 100:.1f}%"


def pp(x):
    return "—" if x is None else f"{x * 100:+.1f}%p"


RESULT_MARK = {"pass": "P", "fail": "F"}
RECOMP = " ※"  # 옛 형식 기록에서 다시 계산한 토큰·비용


def k8_ratio(part):
    if not part:
        return "—"
    r = part.get("ratio")
    return f"{'—' if r is None else f'{r:.3f}'} ({part.get('hangul', 0)}/{part.get('latin', 0)})"


def pooled_k8(runs, key):
    h = sum(((x["k8_by_first_compaction"] or {}).get(key) or {}).get("hangul", 0) for x in runs)
    l = sum(((x["k8_by_first_compaction"] or {}).get(key) or {}).get("latin", 0) for x in runs)
    return {"hangul": h, "latin": l, "ratio": round(h / (h + l), 4) if h + l else None}


def markdown(res):
    """모델마다 따로 표를 낸다(한 실행 폴더에 모델이 여럿이어도 섞지 않는다)."""
    models = sorted({r["model"] for r in res["rows"]})
    if len(models) <= 1:
        return markdown_model(res)
    parts = []
    for m in models:
        sub = {**res, "rows": [r for r in res["rows"] if r["model"] == m],
               "primary": [x for x in res["primary"] if x["model"] == m],
               "pairs": [x for x in res["pairs"] if x["model"] == m],
               "runs": [x for x in res.get("runs", []) if x["model"] == m],
               "k_items": {m: res.get("k_items", {}).get(m, {})},
               "excluded": [x for x in res["excluded"] if x.get("model") == m],
               "per_item": {i: {**v, "cells": {k: b for k, b in v["cells"].items() if k[0] == m}}
                            for i, v in res["per_item"].items() if any(k[0] == m for k in v["cells"])}}
        parts.append(f"## {m}\n\n" + markdown_model(sub))
    return "\n\n".join(parts)


def markdown_model(res):
    rows, out = res["rows"], []
    out += ["### 실행 상태", "",
            "| 모델 | 조건 | 실행 | 측정 완료 | 중단 | 미채점 | 채점 불일치 | 무효 | 입력 불일치 | 집계 대상 | 운영 문제 | 토큰 검출 | "
            "이력 재작성 | 원문 재독 실행 | 자기 메모 실행 |", "|---|---|" + "---|" * 13]
    for r in rows:
        out.append(f"| {r['model']} | {r['condition']} | {r['runs']} | {r['measurement_complete']} | {r['aborted']} | "
                   f"{r['unscored']} | {r['score_mismatch']} | {r['invalid']} | {r['input_mismatch']} | {r['included']} | "
                   f"{r['ops_problems']} | "
                   f"{r['secret_leak']} | {r['history_rewritten']} | {r['reread_runs']} | {r['memo_runs']} |")
    if res["excluded"]:
        out += ["", "집계에서 빠졌거나 운영 문제가 있는 실행:"]
        for x in res["excluded"]:
            why = (["미채점"] if x["unscored"] else []) \
                + [f"채점 파일이 이 실행의 것이 아님 {', '.join(x['score_mismatch'])} — 다시 채점"] * bool(x["score_mismatch"]) \
                + ([] if x["measurement_complete"] else ["측정 미완료"]) \
                + [f"무효 {', '.join(x['invalid_reasons'])}"] * bool(x["invalid_reasons"]) \
                + [f"입력 불일치 {', '.join(x['input_mismatch'])}"] * bool(x["input_mismatch"]) \
                + [f"운영 문제 {', '.join(x['failure_reasons'])}"] * bool(x["failure_reasons"])
            out.append(f"- {x['run_id']}: {' · '.join(why)}")
    out += ["", "### 위반율(집계 대상 실행)", "",
            "| 모델 | 조건 | 위반율(판정 수) | 버릴 후보 ● 위반율(판정 수) | 실행별 위반율 | 판정 제외 | 컴팩트 평균 | "
            "위치 불명 경계 | 프롬프트 직후 경계 | 주입 글자 평균 | 주입 토큰 평균 | 토큰 평균 | 비용 평균(USD) |",
            "|---|---|" + "---|" * 11]
    for r in rows:
        inj = r["condition"] == "outbox"
        out.append(f"| {r['model']} | {r['condition']} | {fmt(r['violation_rate'], True)} ({r['judged']}) | "
                   f"{fmt(r['drop_candidate_rate'], True)} ({r['drop_candidate_judged']}) | "
                   f"{run_rates(r['run_violation_rates'])} | "
                   f"{', '.join(f'{k} {v}' for k, v in sorted(r['not_judged'].items())) or '—'} | "
                   f"{fmt(r['mean_compactions'])} | {r['unlocated_boundaries']} | {r['prompt_adjacent_boundaries']} | "
                   f"{fmt(r['mean_injected_chars'], none='미측정' if inj else '—')} | "
                   f"{fmt(r['mean_injected_tokens'], none='미측정' if inj else '—')} | "
                   f"{fmt(r['mean_tokens'], none='미측정')}{RECOMP * bool(r['spend_recomputed'])} | "
                   f"{fmt(r['mean_cost_usd'], none='미측정')}{RECOMP * bool(r['spend_recomputed'])} |")
    out += ["", "### 주 측정값 — 전달 뒤 컴팩트가 있었던 항목의 위반율 − 같은 모델 대조군의 같은 항목 위반율", "",
            "| 모델 | 조건 | 컴팩트 실행 | 차이를 낸 실행 | 위반율 평균 | 대조군 위반율 평균 | 차이 평균 | 차이 폭 |",
            "|---|---|---|---|---|---|---|---|"]
    for x in res["primary"]:
        out.append(f"| {x['model']} | {x['condition']} | {x['runs']} | {x['runs_with_diff']} | "
                   f"{fmt(x['rate_mean'], True)} | {fmt(x['control_rate_mean'], True)} | {pp(x['diff_mean'])} | "
                   f"{'—' if x['diff_span'] is None else pp(x['diff_span'][0]) + ' ~ ' + pp(x['diff_span'][1])} |")
    out += ["", "| 실행 | 항목 수 | 불명 항목 | 판정 | 대조군별 비교 항목(뺀 항목) | 위반율(같은 항목) | "
            "대조군(실행 수) 평균 [최소~최대] | 차이 |", "|---|---|---|---|---|---|---|---|"]
    for x in res["pairs"]:
        cmp_ = ", ".join(f"{n}({m})" for n, m in zip(x["compared_items"], x["left_out_items"])) or "—"
        nz = f" · 비교 항목 없는 대조군 {x['controls_without_items']}" if x["controls_without_items"] else ""
        out.append(f"| {x['run_id']} | {x['items']} | {x['unknown_items']} | {x['judged']} | {cmp_}{nz} | "
                   f"{fmt(x['rate'], True)} | ({x['controls']}) {fmt(x['control_rate_mean'], True)} "
                   f"[{pct_span(x['control_rate_span'])}] | {pp(x['diff'])} |")
    out += k_tables(res)
    out += standing_table(res)
    per_item = res["per_item"]
    cols = sorted({k for v in per_item.values() for k in v["cells"]})
    out += ["", "### 항목별 — 위반 수/판정 수, 전달 뒤 컴팩트 있음(뒤)·없음·위치 불명(불명)", "",
            "| 항목 | " + " | ".join(f"{m} {c}" for m, c in cols) + " |", "|---|" + "---|" * len(cols)]
    for item in sorted(per_item):
        cells = []
        for k in cols:
            b = per_item[item]["cells"].get(k)
            parts = [f"{lab} {b[key][0]}/{b[key][1]}" for key, lab in (("after", "뒤"), ("none", "없음"),
                                                                       ("unknown", "불명")) if b and b[key][1]]
            cells.append(" · ".join(parts) or "—")
        out.append(f"| {item}{' ●' if per_item[item]['drop_candidate'] else ''} | " + " | ".join(cells) + " |")
    out += ["", "● = 버릴 후보(scoring-items.md 취약 열) — 주 지표·위반율에서 빼고 따로 낸다. 별칭 항목은 세지 않는다.",
            f"※ = 옛 형식 기록(run.json에 usage_accounting 표식 없음 — 턴의 토큰·비용이 세션 누적)에서 턴별 차분으로 다시 계산한 값"
            f"(run.json totals는 턴 수만큼 부풀어 있음)."]
    return "\n".join(out)


def run_rates(xs):
    """실행별 위반율 — 실행이 둘 이상이면 평균을 앞에."""
    vals = ", ".join(fmt(x, True) for x in xs) or "—"
    return f"평균 {fmt(mean(xs), True)} ({vals})" if len(xs) > 1 else vals


def k_tables(res):
    out = ["", "### 제약별(K1~K8) — 위반 실행 수/판정 실행 수 [반복별 P=pass F=fail ·=판정 불가], 집계 대상 실행", ""]
    for model, conds in sorted(res.get("k_items", {}).items()):
        cs = sorted(conds)
        if not cs:
            continue
        out += [f"| 항목 ({model}) | " + " | ".join(cs) + " |", "|---|" + "---|" * len(cs)]
        for i in K_ITEMS:
            cells = []
            for c in cs:
                xs = conds[c].get(i, [])
                f = sum(x == "fail" for _, x in xs)
                n = sum(x in ("pass", "fail") for _, x in xs)
                marks = " ".join(f"r{rep}{RESULT_MARK.get(x, '·')}" for rep, x in xs)
                cells.append(f"{f}/{n} [{marks}]")
            out.append(f"| {i} | " + " | ".join(cells) + " |")
        inc = [x for x in res.get("runs", []) if x["included"] and x["model"] == model]
        out.append("| K8 첫 컴팩트 앞→뒤 한글 비율(한글/라틴 합) | " + " | ".join(
            f"{k8_ratio(pooled_k8([x for x in inc if x['condition'] == c], 'before'))} → "
            f"{k8_ratio(pooled_k8([x for x in inc if x['condition'] == c], 'after'))}" for c in cs) + " |")
        out.append("")
    runs = res.get("runs", [])
    if runs:
        out += ["### 실행별 — 컴팩트·주입·토큰, K8(전체 · 턴 단위 · 첫 컴팩트 앞/뒤 한글 비율(한글/라틴))", "",
                "| 실행 | 조건 | 반복 | 집계 대상 | 컴팩트 | 주입 | 주입 토큰 | 토큰 | K8 (위반 턴) | K8 턴 단위 (위반 턴) | "
                "K8 첫 컴팩트 앞 | K8 첫 컴팩트 뒤 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in runs:
            b = x["k8_by_first_compaction"] or {}
            inj = x["condition"] == "outbox"
            out.append(f"| {x['run_id']} | {x['condition']} | {x['rep']} | {'예' if x['included'] else '아니오'} | "
                       f"{x['compactions']} | {x['injections']} | "
                       f"{fmt(x['injected_tokens'], none='미측정' if inj else '—')} | "
                       f"{fmt(x['tokens'], none='미측정')}{RECOMP * x['spend_recomputed']} | "
                       f"{x['k8'] or '—'} ({len(x['k8_violating_turns'] or [])}) | "
                       f"{x['k8_turn_level'] or '—'} ({len(x['k8_turn_level_violating_turns'] or [])}) | "
                       f"{k8_ratio(b.get('before'))} | {k8_ratio(b.get('after'))} |")
    return out


def standing_table(res):
    """standing 실행의 고정 지시 주입 횟수(훅 호출 수) — standing 실행이 없으면 아무것도 내지 않는다."""
    runs = [x for x in res.get("runs", []) if x["condition"] == "standing"]
    if not runs:
        return []
    out = ["", "### standing 주입 횟수 — 고정 지시 훅 호출 수(run.json standing_counts)", "",
           "| 실행 | 반복 | 집계 대상 | 컴팩트 | UserPromptSubmit | SessionStart·compact | SessionStart 그 밖(source별) | 합 |",
           "|---|---|---|---|---|---|---|---|"]
    for x in runs:
        c = x.get("standing_counts")
        if not c:
            out.append(f"| {x['run_id']} | {x['rep']} | {'예' if x['included'] else '아니오'} | {x['compactions']} | "
                       "미측정 | 미측정 | 미측정 | 미측정 |")
            continue
        ss = c.get("SessionStart") or {}
        rest = ", ".join(f"{k} {v}" for k, v in sorted(ss.items()) if k != "compact") or "—"
        out.append(f"| {x['run_id']} | {x['rep']} | {'예' if x['included'] else '아니오'} | {x['compactions']} | "
                   f"{c.get('UserPromptSubmit')} | {ss.get('compact', 0)} | {rest} | {c.get('total')} |")
    return out


def to_json(res):
    return {**res, "per_item": {i: {"drop_candidate": v["drop_candidate"],
                                    "cells": {f"{m}|{c}": b for (m, c), b in v["cells"].items()}}
                                for i, v in res["per_item"].items()}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="*", help="실행 폴더(또는 그 안의 run.json·score.json)")
    ap.add_argument("--runs-dir", default=str(ROOT / ".runs"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    paths = a.runs or sorted(p.parent for p in Path(a.runs_dir).glob("*/run.json"))
    if not paths:
        print("run.json 없음", file=sys.stderr)
        return 1
    res = summarize(load(paths))
    stale = [x["run_id"] for x in res["excluded"] if x["score_mismatch"]]
    if stale:
        print(f"경고: score.json이 그 폴더 run.json의 채점이 아님(집계에서 뺌, 다시 채점): {stale}", file=sys.stderr)
    print(markdown(res))
    if a.json:
        Path(a.json).write_text(json.dumps(to_json(res), ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
