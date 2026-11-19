#!/usr/bin/env python3
"""고정 지시 훅(standing 조건만 — UserPromptSubmit·SessionStart) — 고정 지시 한 줄을 표준 출력으로 맥락에 넣는다.

사용: `python3 -I standing.py <데이터 폴더> <config.json>`. config.json의 `line`(하네스 v4.5 훅이 내는 문구 그대로)을
줄바꿈 하나와 함께 stdout에 쓴다(UserPromptSubmit·SessionStart의 stdout은 맥락에 들어간다). 매처가 없어 SessionStart는
모든 source(startup·resume·compact 등)에서 불린다.
기록: 부를 때마다 <데이터 폴더>/standing-log.jsonl에 {at, event, source(SessionStart의 source — 그 밖은 null), session_id} 한 줄.
입력(stdin JSON)을 읽지 못해도 주입은 한다(event·source·session_id = null로 기록). 설정을 읽지 못하면 주입하지 않는다.
실패는 <데이터 폴더>/hook-errors.jsonl(hook = "standing")에 남기고 0으로 끝낸다(세션을 죽이지 않음).
과제 파일은 읽지 않는다.
"""
import datetime
import json
import sys
import traceback
from pathlib import Path


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log_row(inp, at=None):
    """standing-log.jsonl 한 줄. inp = 훅 입력 dict(읽지 못했으면 {})."""
    event = inp.get("hook_event_name")
    return {"at": at or now(), "event": event, "source": inp.get("source") if event == "SessionStart" else None,
            "session_id": inp.get("session_id")}


def main():
    data, cfg_path = Path(sys.argv[1]), Path(sys.argv[2])
    raw = sys.stdin.read()
    problems = []
    try:
        line = str(json.loads(cfg_path.read_text(encoding="utf-8"))["line"])
        try:
            inp = json.loads(raw)
            if not isinstance(inp, dict):
                raise ValueError("입력이 JSON 객체가 아님")
        except ValueError:
            problems.append(traceback.format_exc())
            inp = {}
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        data.mkdir(parents=True, exist_ok=True)
        with open(data / "standing-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(log_row(inp), ensure_ascii=False) + "\n")
    except Exception:
        problems.append(traceback.format_exc())
    if problems:
        data.mkdir(parents=True, exist_ok=True)
        with open(data / "hook-errors.jsonl", "a", encoding="utf-8") as f:
            for p in problems:
                f.write(json.dumps({"ts": now(), "hook": "standing", "error": p, "stdin_head": raw[:300]},
                                   ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
