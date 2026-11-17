#!/usr/bin/env python3
"""수집 훅(세 조건 공통) — PreCompact·PostCompact 입력을 실행 기록 파일에 남긴다.

사용: settings.json 훅 명령 `python3 -I compact_log.py <데이터 폴더>`.
- 표준 출력에 아무것도 쓰지 않는다(PreCompact의 stdout은 요약 지시문이 된다 — 맥락에 주입 금지).
- 실패해도 세션을 죽이지 않는다: 오류를 <데이터 폴더>/hook-errors.jsonl에 남기고 0으로 끝낸다.
"""
import datetime
import json
import sys
import traceback
from pathlib import Path


def append(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    data = Path(sys.argv[1])
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw)
        append(data / "compact-log.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": inp.get("hook_event_name"),
            "session_id": inp.get("session_id"),
            "trigger": inp.get("trigger"),
            "custom_instructions": inp.get("custom_instructions"),
            "compact_summary": inp.get("compact_summary"),
        })
    except Exception:
        append(data / "hook-errors.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "hook": "compact_log",
            "error": traceback.format_exc(), "stdin_head": raw[:500]})


if __name__ == "__main__":
    main()
