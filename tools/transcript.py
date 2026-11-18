"""Claude Code 세션 기록(JSONL) 읽기 — 실행기와 채점기가 함께 쓴다.

세션 기록은 한 줄이 JSON 하나이고 덧붙이기만 된다고 가정한다(실행기가 턴마다 사본의 줄 수를 남겨
턴 경계로 쓴다 — 그 가정이 깨지면 실행기가 warnings에 남긴다).
"""
import json
from pathlib import Path


def load(path):
    """(entries, bad_lines). entries[i]는 i번째 줄(파싱 실패 줄은 None)."""
    entries, bad = [], 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append(None)
            bad += 1
    return entries, bad


def _content(e):
    return (e.get("message") or {}).get("content")


def user_text(e):
    """사용자 프롬프트 줄이면 그 본문, 아니면 None(도구 결과·요약·메타·사이드체인 제외)."""
    if not e or e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"):
        return None
    c = _content(e)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


def is_boundary(e):
    return bool(e) and e.get("type") == "system" and e.get("subtype") == "compact_boundary"


def boundaries(entries):
    """[(줄 번호, entry, 요약 줄 존재 여부)]. 요약 여부 = 경계 뒤 첫 대화 줄(user·assistant)이 요약(isCompactSummary)인지.
    2.1.326은 경계와 요약 사이에 `attachment` 줄(session_context·date)을 두므로 대화 줄이 아닌 줄은 건너뛴다(다음 경계까지)."""
    out = []
    for i, e in enumerate(entries):
        if is_boundary(e):
            nxt = None
            for x in entries[i + 1:]:
                if is_boundary(x):
                    break
                if x and x.get("type") in ("user", "assistant"):
                    nxt = x
                    break
            out.append((i, e, bool(nxt and nxt.get("isCompactSummary"))))
    return out


def is_assistant_output(e):
    """어시스턴트 출력 줄(생각·텍스트·도구 호출 무엇이든 — 사이드체인·API 오류 제외)."""
    return bool(e) and e.get("type") == "assistant" and not e.get("isSidechain") and not e.get("isApiErrorMessage")


def tool_uses(entries, start=0, end=None):
    """[(줄 번호, 도구 이름, 입력 dict)] — 어시스턴트 줄의 tool_use 블록."""
    out = []
    for i, e in enumerate(entries[start:end], start):
        if not e or e.get("type") != "assistant":
            continue
        c = _content(e)
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"):
                out.append((i, b.get("name", "?"), b.get("input") or {}))
    return out


def assistant_text(entries, start=0, end=None):
    """[start, end) 범위 어시스턴트 텍스트 블록을 이어 붙인 본문(같은 메시지·같은 글은 한 번만)."""
    seen, parts = set(), []
    for e in entries[start:end]:
        if not e or e.get("type") != "assistant" or e.get("isSidechain") or e.get("isApiErrorMessage"):
            continue
        mid = (e.get("message") or {}).get("id")
        c = _content(e)
        blocks = [c] if isinstance(c, str) else [b.get("text", "") for b in c or []
                                                 if isinstance(b, dict) and b.get("type") == "text"]
        for t in blocks:
            if t and (mid, t) not in seen:
                seen.add((mid, t))
                parts.append(t)
    return "\n\n".join(parts)


def find_prompt(entries, text, start=0, end=None):
    """[start, end)에서 본문이 text와 같은 첫 사용자 프롬프트 줄 번호(없으면 None)."""
    want = text.strip()
    for i, e in enumerate(entries[start:end], start):
        t = user_text(e)
        if t is not None and (t.strip() == want or (len(want) > 80 and t.strip().startswith(want[:80]))):
            return i
    return None


def locate_boundaries(entries, turn_line_ends, messages):
    """경계마다 몇 번째 턴 실행 중에 생겼는지와 그 턴의 프롬프트 앞/뒤를 붙인다.

    turn_line_ends[k] = 턴 k가 끝난 뒤 세션 기록의 줄 수. messages[k] = 턴 k에 보낸 본문.
    position: before_prompt(턴 k의 프롬프트 줄보다 앞) · mid_turn(뒤) · unknown(프롬프트 줄을 못 찾음) ·
    after_last_turn(기록된 턴 범위 밖).
    mid_turn_phase(mid_turn일 때만, 아니면 None — 기록용, 위치 좌표·판정에는 쓰지 않는다): before_first_output(그 턴의 프롬프트
    줄과 경계 사이에 어시스턴트 출력 줄이 없음 — 프롬프트 직후) · after_first_output(있음).
    """
    out = []
    for line, e, has_summary in boundaries(entries):
        turn, lo = None, 0
        for k, hi in enumerate(turn_line_ends):
            if lo <= line < hi:
                turn = k
                break
            lo = hi
        pos, phase = "after_last_turn", None
        if turn is not None:
            p = find_prompt(entries, messages[turn], lo, turn_line_ends[turn]) if turn < len(messages) else None
            pos = "unknown" if p is None else ("before_prompt" if line < p else "mid_turn")
            if pos == "mid_turn":
                phase = ("after_first_output" if any(is_assistant_output(x) for x in entries[p + 1:line])
                         else "before_first_output")
        meta = e.get("compactMetadata") or {}
        out.append({"line": line, "turn": turn, "position": pos, "mid_turn_phase": phase, "timestamp": e.get("timestamp"),
                    "trigger": meta.get("trigger"), "pre_tokens": meta.get("preTokens"),
                    "post_tokens": meta.get("postTokens"), "has_summary": has_summary,
                    "compact_metadata": meta})
    return out


def boundary_coord(b):
    """턴 축 좌표: 턴 k 프롬프트 앞 = k − 0.5, 턴 k 도중 = k. 턴을 모르거나 프롬프트 앞/뒤를 확정하지 못하면
    (position unknown·after_last_turn) None — 이런 경계는 "전달 뒤 컴팩트" 판정에 쓰지 않고 위치 불명으로 따로 센다."""
    if b.get("turn") is None or b.get("position") not in ("before_prompt", "mid_turn"):
        return None
    return b["turn"] - 0.5 if b["position"] == "before_prompt" else float(b["turn"])
