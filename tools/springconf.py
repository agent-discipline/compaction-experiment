"""Spring 설정 파일 읽기 — 최소 YAML/properties 파서 + relaxed binding 키 정규화 + Duration·enum 동치.

범위(이 과제의 application.yml): 들여쓰기 중첩 맵 · 스칼라(따옴표 포함) · 주석 · 점 들어간 키 ·
`---` 다중 문서(프로필 활성화 문서는 건너뜀) · 단순 목록(`- 값`) · 블록 스칼라(|, >) · `${X:기본값}`.
흐름 표기(`{a: b}`, `[a, b]`)는 값 문자열 그대로 둔다. 해석 못 하는 줄은 ConfigError(조용히 넘기지 않음).
"""
import re
from decimal import Decimal
from pathlib import Path


class ConfigError(Exception):
    pass


# ---------- YAML ----------

def _strip_comment(line):
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in ("'", '"') and (i == 0 or line[i - 1] in " \t:-[{,"):
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
        return re.sub(r"\\(.)", lambda m: esc.get(m.group(1), "\\" + m.group(1)), v[1:-1])
    return v


def _split_key(text):
    """'key: value' → (key, value|None). 따옴표 키 허용."""
    m = re.match(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:]+?)\s*:(?:\s+(.*)|$)""", text)
    if not m:
        return None
    return _unquote(m.group(1)), (m.group(2) if m.group(2) is not None else None)


def parse_yaml_docs(text):
    """문서별 평탄화 dict 목록. 값은 문자열(null은 None)."""
    docs, cur = [], []
    for line in text.splitlines():
        if re.match(r"^---\s*(#.*)?$", line):
            docs.append(cur)
            cur = []
        elif re.match(r"^\.\.\.\s*$", line):
            continue
        else:
            cur.append(line)
    docs.append(cur)
    return [_parse_doc(d) for d in docs]


def _parse_doc(lines):
    flat, stack, i = {}, [], 0  # stack: [(indent, key prefix)]
    list_idx = {}
    while i < len(lines):
        raw = lines[i]
        i += 1
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"탭 들여쓰기: {raw!r}")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = stack[-1][1] if stack else ""
        if body.startswith("- ") or body == "-":
            if not prefix:
                raise ConfigError(f"최상위 목록은 지원하지 않음: {raw!r}")
            n = list_idx.get(prefix, 0)
            list_idx[prefix] = n + 1
            flat.pop(prefix, None)
            flat[f"{prefix}[{n}]"] = _scalar(body[1:].strip())
            continue
        kv = _split_key(body)
        if kv is None:
            raise ConfigError(f"해석 못 하는 줄: {raw!r}")
        key, val = kv
        full = f"{prefix}.{key}" if prefix else key
        if val is None or val == "":
            flat[full] = None
            stack.append((indent, full))
        elif val in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip(" ")) > indent):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if val.startswith("|") else " "
            flat[full] = joiner.join(block).strip()
        else:
            flat[full] = _scalar(val)
    # 자식이 있는 키는 None 자리표시를 지운다
    for k in [k for k, v in flat.items() if v is None and any(x.startswith(k + ".") or x.startswith(k + "[")
                                                               for x in flat)]:
        del flat[k]
    return flat


def _scalar(v):
    v = v.strip()
    if v in ("~", "null", "Null", "NULL", ""):
        return None
    return _unquote(v)


def parse_properties(text):
    flat = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"^((?:[^=:\s\\]|\\.)+)\s*[=:\s]\s*(.*)$", line)
        if not m:
            flat[line] = ""
            continue
        flat[m.group(1)] = m.group(2)
    return flat


def _is_profile_doc(flat):
    keys = {normalize_key(k) for k in flat}
    return bool(keys & {"spring.config.activate.onprofile", "spring.profiles"})


def load_spring_config(resources_dir):
    """src/main/resources의 application.yml → .yaml → .properties 순으로 덮어쓴 평탄 dict와 읽은 파일 목록.
    application-*.yml(프로필 파일)은 보지 않는다."""
    d = Path(resources_dir)
    texts = {n: (d / n).read_text(encoding="utf-8") for n in CONFIG_FILES if (d / n).is_file()}
    return load_from_texts(texts)


CONFIG_FILES = ("application.yml", "application.yaml", "application.properties")


def load_from_texts(texts):
    """{파일 이름: 본문} → (평탄 dict, 읽은 파일 목록). 덮어쓰기 순서 yml → yaml → properties."""
    flat, used = {}, []
    for name in CONFIG_FILES:
        if name not in texts:
            continue
        used.append(name)
        if name.endswith(".properties"):
            flat.update(parse_properties(texts[name]))
        else:
            for doc in parse_yaml_docs(texts[name]):
                if not _is_profile_doc(doc):
                    flat.update(doc)
    return flat, used


# ---------- relaxed binding ----------

def normalize_key(k):
    return ".".join(re.sub(r"[-_]", "", seg).lower() for seg in k.split("."))


def lookup(flat, key):
    """(찾음 여부, 값). 같은 정규화 키가 여럿이면 마지막(나중 파일·문서) 값."""
    want, found, val = normalize_key(key), False, None
    for k, v in flat.items():
        if normalize_key(k) == want:
            found, val = True, v
    return found, resolve_placeholder(val)


def resolve_placeholder(v):
    if isinstance(v, str):
        m = re.fullmatch(r"\$\{[^}:]+:([^}]*)\}", v.strip())
        if m:
            return m.group(1)
    return v


# ---------- 값 비교 ----------

_UNITS = {"ns": Decimal("1e-9"), "us": Decimal("1e-6"), "ms": Decimal("0.001"), "s": Decimal(1),
          "m": Decimal(60), "h": Decimal(3600), "d": Decimal(86400)}


def parse_duration(v):
    """Spring Duration 문자열 → 초(Decimal). 해석 못 하면 None."""
    if v is None:
        return None
    s = str(v).strip()
    m = re.fullmatch(r"([+-]?\d+)([a-zA-Z]{0,2})", s)
    if m:
        unit = (m.group(2) or "ms").lower()
        return Decimal(m.group(1)) * _UNITS[unit] if unit in _UNITS else None
    m = re.fullmatch(r"([+-])?P(?:([+-]?\d+)D)?(?:T(?:([+-]?\d+)H)?(?:([+-]?\d+)M)?(?:([+-]?\d+(?:[.,]\d+)?)S)?)?",
                     s, re.I)
    if m and any(m.group(i) for i in range(2, 6)) and not s.upper().endswith("T"):
        total = sum(Decimal(m.group(i).replace(",", ".")) * mul
                    for i, mul in ((2, 86400), (3, 3600), (4, 60), (5, 1)) if m.group(i))
        return -total if m.group(1) == "-" else total
    return None


def norm_enum(v):
    return None if v is None else re.sub(r"[-_\s]", "", str(v)).lower()


def matches(actual, expected, kind):
    """kind: duration(expected=초) · enum · int."""
    if actual is None:
        return False
    if kind == "duration":
        d = parse_duration(actual)
        return d is not None and d == Decimal(str(expected))
    if kind == "enum":
        return norm_enum(actual) == norm_enum(expected)
    if kind == "int":
        try:
            return int(str(actual).strip()) == int(expected)
        except ValueError:
            return False
    raise ValueError(kind)
