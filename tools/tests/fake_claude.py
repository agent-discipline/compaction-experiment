#!/usr/bin/env python3
"""테스트용 가짜 `claude` — 유료 호출 없이 실행기 흐름을 돌린다.

- stdin 프롬프트를 세션 기록(HOME/.claude/projects/-fake/<sid>.jsonl)에 덧붙이고 한국어 응답 한 줄을 쓴다.
- cwd/.claude/settings.json의 훅을 실제처럼 부른다: UserPromptSubmit(매 턴), 그리고
  CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000이고 두 번째 턴부터는 프롬프트 앞에서 컴팩트
  (PreCompact → 경계·요약 줄 → PostCompact → SessionStart(compact)).
- 두 번째 턴에 샌드박스 자격 증명의 expiresAt을 늘린다(되쓰기 확인용).
- FAKE_FAIL_TURN=k 이면 k번째 턴(0부터)에서 exit 1.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.0 (fake)")
    sys.exit(0)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
prompt = sys.stdin.read()
home = Path(os.environ["HOME"])
tp = home / ".claude" / "projects" / "-fake" / f"{sid}.jsonl"
tp.parent.mkdir(parents=True, exist_ok=True)
prev = tp.read_text(encoding="utf-8").splitlines() if tp.exists() else []
turn = sum(1 for x in prev if json.loads(x).get("type") == "user" and isinstance(json.loads(x)["message"]["content"], str)
           and not json.loads(x).get("isCompactSummary"))
if os.environ.get("FAKE_FAIL_TURN") == str(turn):
    print("boom", file=sys.stderr)
    sys.exit(1)
settings = json.loads(Path(".claude/settings.json").read_text(encoding="utf-8"))


def hooks(event, payload, matcher=None):
    outs = []
    for group in settings.get("hooks", {}).get(event, []):
        if matcher and group.get("matcher") not in (None, matcher):
            continue
        for h in group["hooks"]:
            p = subprocess.run(h["command"], shell=True, input=json.dumps(payload), capture_output=True, text=True)
            outs.append(p.stdout)
    return outs


rows = []
if os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == "100000" and turn >= 1:
    hooks("PreCompact", {"session_id": sid, "hook_event_name": "PreCompact", "trigger": "auto"})
    rows.append({"type": "system", "subtype": "compact_boundary", "timestamp": "t",
                 "compactMetadata": {"trigger": "auto", "preTokens": 82000, "postTokens": 9000}})
    rows.append({"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "요약"}})
    hooks("PostCompact", {"session_id": sid, "hook_event_name": "PostCompact", "trigger": "auto",
                          "compact_summary": "요약"})
    hooks("SessionStart", {"session_id": sid, "hook_event_name": "SessionStart", "source": "compact"}, "compact")
hooks("UserPromptSubmit", {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": prompt})
rows.append({"type": "user", "message": {"role": "user", "content": prompt}})
rows.append({"type": "assistant", "message": {"id": f"m{turn}", "role": "assistant", "content": [
    {"type": "text", "text": "요청하신 단계를 구현하고 테스트를 모두 통과시켰습니다."},
    {"type": "tool_use", "name": "Bash", "input": {"command": os.environ.get("FAKE_TOOL_CMD", "ls")}}]}})
with open(tp, "a", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
if turn == 1:
    c = home / ".claude" / ".credentials.json"
    d = json.loads(c.read_text())
    d["claudeAiOauth"]["expiresAt"] += 1000
    c.write_text(json.dumps(d))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "num_turns": 1,
                  "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5,
                                                    "iterations": [{"input_tokens": 10, "cache_read_input_tokens": 90}]},
                  "modelUsage": {"fake": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 85,
                                          "cacheCreationInputTokens": 0}}}))
