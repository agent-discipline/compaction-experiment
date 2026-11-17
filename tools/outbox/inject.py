#!/usr/bin/env python3
"""아웃박스 주입 훅(SessionStart, 매처 compact) — 최근 사용자 메시지 원문과 고정 규칙을 맥락에 넣는다.

사용: `python3 -I inject.py <데이터 폴더> <config.json>`.
읽는 것: 입력(stdin JSON)의 `session_id`·`source`, capture.py가 쌓은 <데이터 폴더>/outbox/<session_id>.jsonl,
config.json(recent_messages=N, max_chars_per_message=L, rule). 과제 파일은 읽지 않는다.
출력: stdout에 {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}.
기록: <데이터 폴더>/inject-log.jsonl 한 줄(설정값·주입 글자 수). 실패 시 hook-errors.jsonl에 남기고
아무것도 주입하지 않은 채 0으로 끝낸다(세션을 죽이지 않음).
"""
import datetime
import hashlib
import json
import re
import sys
import traceback
from pathlib import Path

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def clip(text, limit):
    """앞부분 limit자 + 생략 표시. 생략했는지도 돌려준다."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…(이하 {len(text) - limit}자 생략)", True


def build_context(rows, n, limit, rule):
    """rows = outbox 줄 dict 목록(시간순). (주입문, 쓴 seq 목록, 잘린 메시지 수)."""
    picked = rows[-n:] if n > 0 else []
    parts, cut = [rule], 0
    for r in picked:
        body, clipped = clip(r["prompt"], limit)
        cut += clipped
        parts.append(f"[사용자 메시지 {r['seq']} · {r['ts']}]\n{body}")
    return "\n\n".join(parts), [r["seq"] for r in picked], cut


def main():
    data, cfg_path = Path(sys.argv[1]), Path(sys.argv[2])
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        n, limit, rule = int(cfg["recent_messages"]), int(cfg["max_chars_per_message"]), str(cfg["rule"])
        sid = inp.get("session_id")
        if not isinstance(sid, str) or not SESSION_RE.match(sid):
            raise ValueError(f"session_id 형식 이상: {sid!r}")
        path = data / "outbox" / f"{sid}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"아웃박스 파일 없음: {path.name}")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise ValueError("아웃박스가 비어 있음")
        context, seqs, cut = build_context(rows, n, limit, rule)
        out = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
                         ensure_ascii=False)
        sys.stdout.write(out)
        sys.stdout.flush()
        with open(data / "inject-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), "session_id": sid, "source": inp.get("source"),
                                "recent_messages": n, "max_chars_per_message": limit,
                                "rule_sha256": hashlib.sha256(rule.encode()).hexdigest(),
                                "seqs": seqs, "clipped_messages": cut, "chars_injected": len(context)},
                               ensure_ascii=False) + "\n")
    except Exception:
        data.mkdir(parents=True, exist_ok=True)
        with open(data / "hook-errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), "hook": "inject", "error": traceback.format_exc(),
                                "stdin_head": raw[:300]}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
