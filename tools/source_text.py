"""소스 본문 정리 — Kotlin 주석 제거·문자열 리터럴 추출, SQL 주석 제거, 트리 단위 본문 모음."""
from pathlib import Path

TEXT_SUFFIXES = {".kt", ".kts", ".java", ".sql", ".yml", ".yaml", ".properties", ".xml", ".json", ".txt"}


def kotlin_scan(src):
    """(주석을 지운 본문, 문자열 리터럴 목록). 줄 바꿈은 남겨 줄 번호를 보존한다.
    처리: // 줄 주석 · /* */ 중첩 블록 주석 · "…"(이스케이프·${…} 템플릿) · \"\"\"…\"\"\" · 'c'."""
    out, lits, i, n = [], [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(" " + "\n" * src.count("\n", i, j))
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j
            while j + 3 < n and src[j + 3] == '"':  # """…"""" 끝의 여분 따옴표는 본문
                j += 1
            lits.append(src[i + 3:j])
            out.append(src[i:j + 3])
            i = j + 3
        elif c == '"':
            j = i + 1
            while j < n and src[j] not in '"\n':
                if src[j] == "\\":
                    j += 2
                elif src.startswith("${", j):
                    depth, j = 1, j + 2
                    while j < n and depth:
                        depth += {"{": 1, "}": -1}.get(src[j], 0)
                        j += 1
                else:
                    j += 1
            lits.append(src[i + 1:j])
            out.append(src[i:j + 1])
            i = j + 1
        elif c == "'":
            j = src.find("'", i + 1 + (src[i + 1:i + 2] == "\\"))
            if 0 < j - i <= 8:
                out.append(src[i:j + 1])
                i = j + 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out), lits


def strip_kotlin(src):
    return kotlin_scan(src)[0]


def strip_sql(src):
    """-- 줄 주석과 /* */ 주석 제거(작은따옴표 문자열 안은 보존)."""
    out, i, n, q = [], 0, len(src), False
    while i < n:
        c = src[i]
        if q:
            out.append(c)
            if c == "'":
                q = False
            i += 1
        elif c == "'":
            q = True
            out.append(c)
            i += 1
        elif src.startswith("--", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " + "\n" * src.count("\n", i, j))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def clean(path, text):
    s = Path(path).suffix
    if s in (".kt", ".kts", ".java"):
        return strip_kotlin(text)
    if s == ".sql":
        return strip_sql(text)
    return text


def corpus(base, sub="src/main"):
    """{상대 경로: 주석 지운 본문} — base/sub 아래 텍스트 파일."""
    base = Path(base)
    out = {}
    root = base / sub
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in TEXT_SUFFIXES:
            out[p.relative_to(base).as_posix()] = clean(p, p.read_text(encoding="utf-8", errors="replace"))
    return out


def corpus_from_files(files):
    """{상대 경로: 원문} → {상대 경로: 주석 지운 본문} (git 트리에서 읽은 파일용)."""
    return {k: clean(k, v) for k, v in files.items() if Path(k).suffix in TEXT_SUFFIXES}
