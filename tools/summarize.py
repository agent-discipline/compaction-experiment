#!/usr/bin/env python3
"""집계 — 여러 실행의 score.json을 조건·모델별 표로.

사용: python3 tools/summarize.py [score.json ...] [--runs-dir .runs] [--json out.json]
인자가 없으면 <runs-dir>/*/score.json 전부. 출력: 표준 출력에 Markdown 표 2개.
- 위반율 = fail / (pass + fail). not_run·unscorable·premise_unmet는 분모에서 빼고 따로 센다.
- 위반율은 유효(invalid 아님)이고 끝난(status complete) 실행만으로 낸다. 무효·중단 수는 따로 적는다.
- alias_of가 있는 항목(같은 테스트의 별칭)은 세지 않는다.
- "컴팩트 뒤" 위반율 = 전달 뒤 컴팩트가 1회 이상 있었던 항목만(compactions_after_delivery ≥ 1).
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rate(f, p):
    return None if f + p == 0 else f / (f + p)


def fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else (f"{x:.1f}" if isinstance(x, float) else str(x))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def summarize(scores):
    groups = {}
    for s in scores:
        groups.setdefault((s["model"], s["condition"]), []).append(s)
    rows, per_item = [], {}
    for (model, cond), ss in sorted(groups.items()):
        ok = [s for s in ss if s["status"] == "complete" and not s.get("invalid")]
        f = p = fa = pa = 0
        other = {}
        for s in ok:
            for item, r in s["items"].items():
                if r.get("alias_of"):  # 같은 테스트의 별칭 항목(D6-04 = D0-13)은 한 번만 센다
                    continue
                res = r["result"]
                cell = per_item.setdefault(item, {}).setdefault((model, cond), [0, 0])
                if res in ("pass", "fail"):
                    f, p = f + (res == "fail"), p + (res == "pass")
                    cell[0] += res == "fail"
                    cell[1] += 1
                    if (r.get("compactions_after_delivery") or 0) >= 1:
                        fa, pa = fa + (res == "fail"), pa + (res == "pass")
                else:
                    other[res] = other.get(res, 0) + 1
        rows.append({
            "model": model, "condition": cond, "runs": len(ss), "valid_complete": len(ok),
            "invalid": sum(bool(s.get("invalid")) for s in ss),
            "aborted": sum(s["status"] in ("aborted", "failed") for s in ss),  # failed = 토큰 검출·정리 실패 등
            "violation_rate": rate(f, p), "judged": f + p,
            "violation_rate_after_compaction": rate(fa, pa), "judged_after_compaction": fa + pa,
            "not_judged": other,
            "mean_compactions": mean([s.get("compactions") for s in ok]),
            "mean_injected_chars": mean([s.get("injected_chars") for s in ok]),
            "mean_tokens": mean([(s.get("totals") or {}).get("tokens") for s in ok]),
            "run_violation_rates": [rate(sum(r["result"] == "fail" for r in s["items"].values()),
                                         sum(r["result"] == "pass" for r in s["items"].values())) for s in ok],
        })
    return rows, per_item


def markdown(rows, per_item):
    out = ["| 모델 | 조건 | 실행 | 유효·완료 | 무효 | 중단 | 위반율(판정 수) | 컴팩트 뒤 위반율(판정 수) | 실행별 위반율 | "
           "판정 제외 | 컴팩트 평균 | 주입 글자 평균 | 토큰 평균 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['model']} | {r['condition']} | {r['runs']} | {r['valid_complete']} | {r['invalid']} | "
                   f"{r['aborted']} | {fmt(r['violation_rate'], True)} ({r['judged']}) | "
                   f"{fmt(r['violation_rate_after_compaction'], True)} ({r['judged_after_compaction']}) | "
                   f"{', '.join(fmt(x, True) for x in r['run_violation_rates']) or '—'} | "
                   f"{', '.join(f'{k} {v}' for k, v in sorted(r['not_judged'].items())) or '—'} | "
                   f"{fmt(r['mean_compactions'])} | {fmt(r['mean_injected_chars'])} | {fmt(r['mean_tokens'])} |")
    cols = sorted({k for v in per_item.values() for k in v})
    out += ["", "| 항목 | " + " | ".join(f"{m} {c}" for m, c in cols) + " |", "|---|" + "---|" * len(cols)]
    for item in sorted(per_item):
        cells = [f"{per_item[item][k][0]}/{per_item[item][k][1]}" if k in per_item[item] else "—" for k in cols]
        out.append(f"| {item} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("항목 표의 칸 = 위반 수/판정 수(유효·완료 실행만).")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scores", nargs="*")
    ap.add_argument("--runs-dir", default=str(ROOT / ".runs"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    paths = [Path(x) for x in a.scores] or sorted(Path(a.runs_dir).glob("*/score.json"))
    if not paths:
        print("score.json 없음", file=sys.stderr)
        return 1
    scores = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    rows, per_item = summarize(scores)
    print(markdown(rows, per_item))
    if a.json:
        Path(a.json).write_text(json.dumps({"rows": rows, "per_item": {
            i: {f"{m}|{c}": v for (m, c), v in d.items()} for i, d in per_item.items()}}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
