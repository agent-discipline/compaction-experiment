"""DB 카탈로그 항목(D0-01·D1-01·D1-02·D1-04·D4-01·D6-01·D7-02) — 최종 마이그레이션을 빈 PostgreSQL에 적용하고
information_schema·pg_catalog를 조회해 판정한다.

방식: `docker run postgres:16` 일회용 컨테이너 → V<n>__*.sql을 번호 순으로 `psql --single-transaction
-v ON_ERROR_STOP=1`로 적용(Flyway 대신 — 파일 하나 = 트랜잭션 하나, 첫 실패에서 멈춤) → 조회 → 컨테이너 삭제.
"""
import re
import subprocess
import time
import uuid
from pathlib import Path

S0_COLUMNS = {
    ("product", "id"): ("bigint", None), ("product", "name"): ("character varying", 200),
    ("product_schedule", "id"): ("bigint", None), ("product_schedule", "product_id"): ("bigint", None),
    ("product_schedule", "starts_at"): ("timestamp with time zone", None),
    ("product_seat", "id"): ("bigint", None), ("product_seat", "schedule_id"): ("bigint", None),
    ("product_seat", "section"): ("character varying", 20), ("product_seat", "row_no"): ("integer", None),
    ("product_seat", "seat_no"): ("integer", None), ("product_seat", "status"): ("character varying", 20),
    ("seat_hold", "id"): ("bigint", None), ("seat_hold", "seat_id"): ("bigint", None),
    ("seat_hold", "schedule_id"): ("bigint", None), ("seat_hold", "user_id"): ("bigint", None),
    ("seat_hold", "held_at"): ("timestamp with time zone", None),
    ("seat_hold", "expires_at"): ("timestamp with time zone", None),
    ("reservation", "id"): ("bigint", None), ("reservation", "schedule_id"): ("bigint", None),
    ("reservation", "seat_id"): ("bigint", None), ("reservation", "user_id"): ("bigint", None),
    ("reservation", "payment_uid"): ("character varying", 64), ("reservation", "status"): ("character varying", 20),
    ("reservation", "created_at"): ("timestamp with time zone", None),
}
S1_INDEXES = {
    "idx_seat_hold_seat_id": ("seat_hold", "seat_id"), "idx_seat_hold_user_id": ("seat_hold", "user_id"),
    "idx_seat_hold_expires_at": ("seat_hold", "expires_at"),
    "idx_reservation_schedule_user": ("reservation", "schedule_id, user_id"),
    "idx_reservation_seat_id": ("reservation", "seat_id"),
}
ALLOWED_UNIQUE = {"uq_reservation_confirmed_seat"}
QUERIES = {
    "columns": "SELECT table_name, column_name, data_type, coalesce(character_maximum_length::text, ''), "
               "is_nullable, coalesce(column_default, '') FROM information_schema.columns WHERE table_schema = 'public'",
    "indexes": "SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisunique, i.indisprimary, t.relname "
               "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
               "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'",
    "constraints": "SELECT con.conname, con.contype, t.relname, pg_get_constraintdef(con.oid) FROM pg_constraint con "
                   "JOIN pg_class t ON t.oid = con.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                   "WHERE n.nspname = 'public'",
}


def migration_files(migration_dir):
    d = Path(migration_dir)
    files = [p for p in d.glob("*.sql") if re.match(r"V(\d+)__", p.name)] if d.is_dir() else []
    return sorted(files, key=lambda p: int(re.match(r"V(\d+)__", p.name).group(1)))


def _docker(*args, stdin=None, timeout=120):
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)


def collect(migration_dir, image="postgres:16"):
    """{applied, migration_error, columns, indexes, constraints}. docker 실패는 예외로 올린다."""
    name = f"cx-catalog-{uuid.uuid4().hex[:8]}"
    p = _docker("run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=x", image, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"docker run 실패: {p.stderr.strip()[:300]}")
    try:
        for _ in range(120):
            if _docker("exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1").returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("postgres 준비 안 됨")
        psql = ["exec", "-i", name, "psql", "-U", "postgres", "-h", "127.0.0.1", "-X", "-q", "-v", "ON_ERROR_STOP=1"]
        out = {"applied": [], "migration_error": None}
        for f in migration_files(migration_dir):
            r = _docker(*psql, "--single-transaction", "-f", "-", stdin=f.read_text(encoding="utf-8"))
            if r.returncode != 0:
                out["migration_error"] = {"file": f.name, "stderr": r.stderr.strip()[:800]}
                break
            out["applied"].append(f.name)
        for key, q in QUERIES.items():
            r = _docker(*psql, "-At", "-F", "\x1f", "-c", q)
            if r.returncode != 0:
                raise RuntimeError(f"조회 실패({key}): {r.stderr.strip()[:300]}")
            out[key] = [line.split("\x1f") for line in r.stdout.splitlines() if line]
        return out
    finally:
        _docker("rm", "-f", name)


def _R(result, **kw):
    return {"result": result, **kw}


def check(cat):
    """카탈로그 → {항목: {result, ...}}."""
    cols = {(r[0], r[1]): {"type": r[2], "len": int(r[3]) if r[3] else None, "nullable": r[4], "default": r[5]}
            for r in cat["columns"]}
    idx = {r[0]: {"def": r[1], "unique": r[2] == "t", "primary": r[3] == "t", "table": r[4]} for r in cat["indexes"]}
    cons = [{"name": r[0], "type": r[1], "table": r[2], "def": r[3]} for r in cat["constraints"]]
    out = {}
    miss = [f"{t}.{c}" for (t, c), (ty, ln) in S0_COLUMNS.items()
            if (t, c) not in cols or cols[(t, c)]["type"] != ty or (ln is not None and cols[(t, c)]["len"] != ln)]
    out["D0-01"] = _R("fail" if miss else "pass", mismatched=miss)
    bad1, bad2 = [], []
    for name, (table, colist) in S1_INDEXES.items():
        i = idx.get(name)
        pat = rf"^CREATE INDEX {name} ON (\w+\.)?{table} USING btree \({re.escape(colist)}\)$"
        if not i or not re.match(pat, i["def"]):
            bad1.append({"index": name, "def": i["def"] if i else None})
        if not i or i["unique"]:
            bad2.append(name)
    out["D1-01"] = _R("fail" if bad1 else "pass", mismatched=bad1)
    out["D1-02"] = _R("fail" if bad2 else "pass", unique_or_missing=bad2)
    uniq = {n for n, i in idx.items() if i["unique"] and not i["primary"] and i["table"] != "flyway_schema_history"}
    uniq |= {c["name"] for c in cons if c["type"] == "u" and c["table"] != "flyway_schema_history"}
    out["D1-04"] = _R("fail" if uniq - ALLOWED_UNIQUE else "pass", extra_unique=sorted(uniq - ALLOWED_UNIQUE))
    q = {c: cols.get(("user_hold_quota", c)) for c in ("schedule_id", "user_id", "cnt")}
    pk = [c["def"] for c in cons if c["table"] == "user_hold_quota" and c["type"] == "p"]
    chk = [c["def"] for c in cons if c["table"] == "user_hold_quota" and c["type"] == "c"]
    ok = (all(q.values()) and q["schedule_id"]["type"] == q["user_id"]["type"] == "bigint"
          and q["cnt"]["type"] == "integer" and all(v["nullable"] == "NO" for v in q.values())
          and q["cnt"]["default"].strip("()") == "0"
          and pk == ["PRIMARY KEY (schedule_id, user_id)"] and any(re.search(r"cnt\s*>=\s*0", c) for c in chk))
    out["D4-01"] = _R("pass" if ok else "fail", columns=q, primary_key=pk, checks=chk)
    i = idx.get("uq_reservation_confirmed_seat")
    norm = re.sub(r"\s+", " ", re.sub(r"::(text|character varying)|[()]", "", i["def"])).strip() if i else None
    good = bool(norm) and re.match(r"^CREATE UNIQUE INDEX uq_reservation_confirmed_seat ON (\w+\.)?reservation "
                                   r"USING btree seat_id WHERE status = 'CONFIRMED'$", norm)
    out["D6-01"] = _R("pass" if good else "fail", indexdef=i["def"] if i else None, normalized=norm)
    e = cols.get(("seat_hold", "extended_at"))
    out["D7-02"] = _R("pass" if e and e["type"] == "timestamp with time zone" and e["nullable"] == "YES" else "fail",
                      column=e)
    if cat.get("migration_error"):
        for r in out.values():
            r["migration_error"] = cat["migration_error"]
    return out
