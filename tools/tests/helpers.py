"""테스트 공용 — tools/를 import 경로에 넣고 가짜 세션 기록·작업 폴더를 만든다."""
import json
import os
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def user(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def assistant(text=None, tools=(), mid="m"):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return {"type": "assistant", "message": {"id": mid, "role": "assistant", "content": content}}


def tool_result():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def boundary(trigger="auto", pre=82000, post=9000):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-18T00:00:00Z",
            "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": post}}


def summary():
    return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "요약"}}


def git_env(date):
    return dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.invalid", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@e.invalid", GIT_AUTHOR_DATE=f"@{date} +0000", GIT_COMMITTER_DATE=f"@{date} +0000",
                HOME=os.environ.get("HOME", "/tmp"))


def git(cwd, *args, date=1700000000):
    subprocess.run(["git", *args], cwd=cwd, env=git_env(date), check=True, capture_output=True)


def write(base, rel, text):
    p = Path(base) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
