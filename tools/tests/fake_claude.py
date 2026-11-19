#!/usr/bin/env python3
"""테스트용 가짜 `claude` — 유료 호출 없이 실행기 흐름을 돌린다(가짜 docker가 컨테이너 대신 호스트에서 부른다).

- stdin 프롬프트를 세션 기록(CLAUDE_CONFIG_DIR/projects/-work/<sid>.jsonl)에 덧붙이고 한국어 응답 한 줄을 쓴다.
- cwd/.claude/settings.json의 훅을 실제처럼 부른다(명령 속 컨테이너 경로는 FAKE_PATH_MAP으로 호스트 경로로 바꿈):
  SessionStart(매 호출 — 첫 턴 startup, 그 뒤 resume), UserPromptSubmit(매 턴), 그리고 CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000이고 두 번째 턴부터는 프롬프트 앞에서 컴팩트
  (PreCompact → 경계·요약 줄 → PostCompact → SessionStart(compact)).
- FAKE_FAIL_TURN=k 이면 k번째 턴(0부터)에서 exit 1. FAKE_LEAK_TURN=k 이면 그 턴에 환경의 토큰 값을 작업 폴더 파일에 쓴다.
  FAKE_COMMIT_LEAK_TURN=k 이면 그 턴에 토큰 값이 든 파일을 git에 커밋한 뒤 작업 트리에서는 지운다(git 객체에만 남음).
  FAKE_BAD_LINE_TURN=k 이면 그 턴 끝에 JSON이 아닌 줄을 덧붙인다. FAKE_TOOL_CMD = 어시스턴트 Bash 도구 입력.
  FAKE_MEMORY_TURN=k 이면 그 턴에 자동 메모리 파일(CLAUDE_CONFIG_DIR/projects/-work/memory/MEMORY.md)을 쓴다(꺼져 있어야 할 때 생긴 경우 흉내).
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
config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(os.environ["HOME"]) / ".claude")
tp = config_dir / "projects" / "-work" / f"{sid}.jsonl"
tp.parent.mkdir(parents=True, exist_ok=True)
prev = tp.read_text(encoding="utf-8").splitlines() if tp.exists() else []
turn = sum(1 for x in prev if json.loads(x).get("type") == "user" and isinstance(json.loads(x)["message"]["content"], str)
           and not json.loads(x).get("isCompactSummary"))
if os.environ.get("FAKE_FAIL_TURN") == str(turn):
    print("boom", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAKE_LEAK_TURN") == str(turn):
    Path("leak.txt").write_text("token=" + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "") + "\n")
if os.environ.get("FAKE_COMMIT_LEAK_TURN") == str(turn):
    Path("cfg.txt").write_text("token=" + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "") + "\n")
    g = ["git", "-c", "safe.directory=*", "-c", "user.name=a", "-c", "user.email=a@e.invalid"]
    subprocess.run([*g, "add", "cfg.txt"], check=True, capture_output=True)
    subprocess.run([*g, "commit", "-q", "-m", "[S0] x"], check=True, capture_output=True)
    subprocess.run([*g, "rm", "-q", "cfg.txt"], check=True, capture_output=True)
    subprocess.run([*g, "commit", "-q", "-m", "[S0] y"], check=True, capture_output=True)
if os.environ.get("FAKE_MEMORY_TURN") == str(turn):
    (tp.parent / "memory").mkdir(exist_ok=True)
    (tp.parent / "memory" / "MEMORY.md").write_text("- 규칙\n", encoding="utf-8")
settings = json.loads(Path(".claude/settings.json").read_text(encoding="utf-8"))
path_map = json.loads(os.environ.get("FAKE_PATH_MAP", "[]"))


def to_host(command):
    for dst, src in path_map:
        command = command.replace(dst + "/", src + "/").replace(dst + " ", src + " ")
        if command.endswith(dst):
            command = command[:-len(dst)] + src
    return command


def hooks(event, payload, matcher=None):
    outs = []
    for group in settings.get("hooks", {}).get(event, []):
        if matcher and group.get("matcher") not in (None, matcher):
            continue
        for h in group["hooks"]:
            p = subprocess.run(to_host(h["command"]), shell=True, input=json.dumps(payload), capture_output=True,
                               text=True)
            outs.append(p.stdout)
    return outs


rows = []
# 실제 CLI처럼 호출마다 세션 시작 훅(첫 턴 startup, 그 뒤 resume) — 매처가 compact인 outbox 훅은 여기서 불리지 않는다
hooks("SessionStart", {"session_id": sid, "hook_event_name": "SessionStart", "source": "resume" if turn else "startup"},
      "resume" if turn else "startup")
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
    if os.environ.get("FAKE_BAD_LINE_TURN") == str(turn):  # 읽지 못하는 줄(마지막 턴에만 쓸 것 — 다음 턴이 못 센다)
        f.write("{not json\n")
# 실제 CLI(2.1.277+)처럼 --resume 턴의 modelUsage·total_cost_usd는 세션 누적, usage는 그 호출의 주 루프만(턴마다 100토큰·0.01달러)
n = turn + 1
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": sid, "num_turns": 1,
                  "total_cost_usd": round(0.01 * n, 6), "usage": {"input_tokens": 10, "output_tokens": 5,
                                                    "iterations": [{"input_tokens": 10, "cache_read_input_tokens": 90}]},
                  "modelUsage": {"fake": {"inputTokens": 10 * n, "outputTokens": 5 * n, "cacheReadInputTokens": 85 * n,
                                          "cacheCreationInputTokens": 0}}}))
