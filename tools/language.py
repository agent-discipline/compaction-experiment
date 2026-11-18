"""응답 언어(K8) — 어시스턴트 본문에서 코드·인용·URL·경로를 뺀 뒤 한글 비율을 잰다."""
import re

FENCE = re.compile(r"(^|\n)[ \t]*(```|~~~)[^\n]*\n.*?(\n[ \t]*\2[^\n]*(?=\n|$)|$)", re.S)
INLINE = re.compile(r"`[^`\n]*`")
QUOTE = re.compile(r"^[ \t]*>.*$", re.M)
URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
# 경로는 토큰 전체를 지운다(한글 파일·폴더 이름 포함). 한글만으로 된 "선점/확정" 같은 산문은 경로가 아니다 — 경로로 보는 것:
#  ① 확장자로 끝나는 `…/이름.ext`(폴더·파일 이름 한글 가능) ② `/`·`./`·`../`·`~/` 뒤 ASCII 폴더로 시작
#  ③ ASCII 폴더 둘 이상으로 시작 ④ 확장자가 붙은 파일 이름 하나 ⑤ ASCII만의 `a/b`(전과 같음)
EXT = r"(?:kt|kts|java|sql|ya?ml|md|json|xml|properties|gradle|txt|sh|py)(?![A-Za-z0-9_])"
SEG = r"[^\s/()\[\]{}<>\"'`,;:]"   # 경로 한 조각의 글자(한글 포함, 공백·구분 기호 제외)
ASEG = r"[A-Za-z0-9_.@-]+"
PATH = re.compile(rf"(?<![\w가-힣])(?:"
                  rf"(?:(?:~|\.{{1,2}})?/)?(?:{SEG}+/)+{SEG}*?\.{EXT}"
                  rf"|(?:~|\.{{1,2}})?/{ASEG}(?:/{SEG}*)*"
                  rf"|(?:{ASEG}/){{2,}}{SEG}*(?:/{SEG}*)*"
                  rf"|{SEG}+?\.{EXT}"
                  rf"|(?:\.{{0,2}}/)?(?:{ASEG}/)+[A-Za-z0-9_.@-]*)")
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
