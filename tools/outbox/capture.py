#!/usr/bin/env python3
"""아웃박스 기록 훅(UserPromptSubmit) — 받은 사용자 메시지를 세션별 파일에 덧붙인다.

사용: `python3 -I capture.py <데이터 폴더>` · 입력(stdin JSON)의 `session_id`·`prompt`만 쓴다.
출력: <데이터 폴더>/outbox/<session_id>.jsonl 한 줄 {ts, seq, prompt}.
- 표준 출력에 아무것도 쓰지 않는다(UserPromptSubmit의 stdout은 맥락에 들어간다).
- 실패해도 프롬프트를 막지 않는다: 오류는 <데이터 폴더>/hook-errors.jsonl, 종료 코드 0.
"""
import datetime
import fcntl
import json
import re
import sys
import traceback
from pathlib import Path

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def outbox_file(data, session_id):
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        raise ValueError(f"session_id 형식 이상: {session_id!r}")
    return data / "outbox" / f"{session_id}.jsonl"


def append_prompt(data, session_id, prompt, ts=None):
    if not isinstance(prompt, str):
        raise ValueError("prompt 없음")
    path = outbox_file(data, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        seq = sum(1 for line in f if line.strip()) + 1
        f.write(json.dumps({"ts": ts or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "seq": seq, "prompt": prompt}, ensure_ascii=False) + "\n")
    return seq


def main():
    data = Path(sys.argv[1])
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw)
        append_prompt(data, inp.get("session_id"), inp.get("prompt"))
    except Exception:
        data.mkdir(parents=True, exist_ok=True)
        with open(data / "hook-errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "hook": "capture",
                                "error": traceback.format_exc(), "stdin_head": raw[:300]}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
