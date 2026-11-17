"""응답 언어(K8) — 어시스턴트 본문에서 코드·인용·URL·경로를 뺀 뒤 한글 비율을 잰다."""
import re

FENCE = re.compile(r"(^|\n)[ \t]*(```|~~~)[^\n]*\n.*?(\n[ \t]*\2[^\n]*(?=\n|$)|$)", re.S)
INLINE = re.compile(r"`[^`\n]*`")
QUOTE = re.compile(r"^[ \t]*>.*$", re.M)
URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
PATH = re.compile(r"(?<![\w가-힣])(?:\.{0,2}/)?(?:[\w.@-]+/)+[\w.@-]*"
                  r"|(?<![\w가-힣])[\w-]+\.(?:kt|kts|java|sql|ya?ml|md|json|xml|properties|gradle|txt|sh|py)\b")
HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
LATIN = re.compile(r"[A-Za-z]")


def strip_non_prose(text):
    t = FENCE.sub("\n", text)
    t = INLINE.sub(" ", t)
    t = QUOTE.sub(" ", t)
    t = URL.sub(" ", t)
    return PATH.sub(" ", t)


def ratio(text, min_chars=20):
    """{prose_chars, hangul, latin, ratio(None=판정 제외)}. prose_chars = 공백 뺀 본문 글자 수."""
    prose = strip_non_prose(text)
    chars = len(re.sub(r"\s", "", prose))
    h, l = len(HANGUL.findall(prose)), len(LATIN.findall(prose))
    r = h / (h + l) if (h + l) and chars >= min_chars else None
    return {"prose_chars": chars, "hangul": h, "latin": l, "ratio": None if r is None else round(r, 4)}
