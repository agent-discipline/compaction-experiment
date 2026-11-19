#!/usr/bin/env python3
"""D6-06 한 항목 재채점 이식(일회성, 2026-11-19).

배경: D6-06 둘째 정규식이 값을 문장에 쓴 형태만 받아 바인드 변수로 쓴 같은 조건부 UPDATE(haiku control r2·r3)를 위반으로
채점했다. 정규식을 고친 뒤(tools/score_run.py REGEX_ALL["D6-06"]) 숨긴 테스트·DB 카탈로그를 다시 돌리지 않고 이 항목만 바꾼다.
근거: docs/plans/2026-11-18/compaction-retention/requirement-spec.md 끝 줄.

실행(저장소 루트에서):
  python3 docs/results/main-2026-11-18/rescore-D6-06.py .runs/haiku-5-5-* .runs/sonnet-5-5-*
실행 폴더마다:
  1) score.json → score.main-orig.json 복사(이미 있으면 덮지 않음)
  2) tools/score_run.py --skip-hidden --skip-db --out <임시 파일>로 채점(Docker 안 씀) → items["D6-06"]만 가져온다
  3) score.main-orig.json을 바탕으로 items["D6-06"] 교체 · counts 재계산 · 최상위 rescored_items 기록(바뀌지 않아도 before=after)
  4) 확인: 위 세 곳을 뺀 나머지가 원본과 같은지(직렬화 바이트 비교), 원본을 다시 직렬화하면 원본 파일 바이트와 같은지
     실패하면 그 폴더의 score.json을 쓰지 않고 종료 코드 1.
다시 돌려도 결과가 같다(항상 score.main-orig.json에서 출발).
"""
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCORER = ROOT / "tools" / "score_run.py"
ITEM = "D6-06"
REASON = "정규식 보정(바인드 변수 형태) — requirement-spec 2026-11-19"


def dump(d):
    return json.dumps(d, ensure_ascii=False, indent=1)


def rescore(run_dir, scorer_sha, tmp):
    run_dir = Path(run_dir)
    cur, orig = run_dir / "score.json", run_dir / "score.main-orig.json"
    if not orig.exists():
        shutil.copy2(cur, orig)
    raw = orig.read_text(encoding="utf-8")
    base = json.loads(raw)
    if dump(base) != raw:
        return f"{run_dir.name}: 원본 재직렬화가 원본 바이트와 다름(쓰지 않음)"
    out = Path(tmp) / f"{run_dir.name}.json"
    subprocess.run([sys.executable, str(SCORER), "--run", str(run_dir), "--skip-hidden", "--skip-db",
                    "--out", str(out)], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
    new = json.loads(out.read_text(encoding="utf-8"))
    if new["run_sha256"] != base["run_sha256"]:
        return f"{run_dir.name}: run_sha256 불일치(쓰지 않음)"
    merged = json.loads(raw)
    before, after = base["items"][ITEM]["result"], new["items"][ITEM]["result"]
    merged["items"][ITEM] = new["items"][ITEM]
    counts = {}
    for r in merged["items"].values():
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    merged["counts"] = counts
    merged["rescored_items"] = {ITEM: {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                                       "reason": REASON, "before": before, "after": after,
                                       "scorer_sha": scorer_sha}}

    def rest(d):
        d = json.loads(dump(d))
        d["items"].pop(ITEM)
        d.pop("counts")
        d.pop("rescored_items", None)
        return dump(d)
    if rest(merged) != rest(base):
        return f"{run_dir.name}: D6-06·counts·rescored_items 밖의 차이(쓰지 않음)"
    cur.write_text(dump(merged), encoding="utf-8")
    print(f"{run_dir.name}: {ITEM} {before} -> {after} · counts {base['counts']} -> {counts}")
    return None


def main(argv):
    scorer_sha = hashlib.sha256(SCORER.read_bytes()).hexdigest()
    errors = []
    with tempfile.TemporaryDirectory(prefix="cx-rescore-") as tmp:
        for d in argv:
            err = rescore(d, scorer_sha, tmp)
            if err:
                errors.append(err)
                print(err, file=sys.stderr)
    print(f"scorer_sha={scorer_sha}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
