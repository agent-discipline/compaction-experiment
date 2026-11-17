import re
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from helpers import TOOLS, write

import containers
import db_catalog as dc
import run_task as rt


def s0_sql():
    md = (TOOLS.parent / "tasks/seat-reservation/steps/S0.md").read_text(encoding="utf-8")
    return re.search(r"```sql\n(.*?)```", md, re.S).group(1)


V2 = """CREATE INDEX idx_seat_hold_seat_id ON seat_hold (seat_id);
CREATE INDEX idx_seat_hold_user_id ON seat_hold (user_id);
CREATE INDEX idx_seat_hold_expires_at ON seat_hold (expires_at);
CREATE INDEX idx_reservation_schedule_user ON reservation (schedule_id, user_id);
CREATE INDEX idx_reservation_seat_id ON reservation (seat_id);
"""
V3 = """CREATE TABLE user_hold_quota (
    schedule_id BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    cnt         INT    NOT NULL DEFAULT 0 CHECK (cnt >= 0),
    PRIMARY KEY (schedule_id, user_id)
);
CREATE UNIQUE INDEX uq_reservation_confirmed_seat ON reservation (seat_id) WHERE status = 'CONFIRMED';
ALTER TABLE seat_hold ADD COLUMN extended_at TIMESTAMPTZ NULL;
"""


def docker_ok():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class CheckTest(unittest.TestCase):
    def test_empty_catalog_fails_everything(self):
        r = dc.check({"columns": [], "indexes": [], "constraints": []})
        self.assertEqual({k for k, v in r.items() if v["result"] != "fail"}, {"D1-04"})  # 유니크 없음 = 위반 없음

    def test_migration_order_numeric(self):
        d = Path(tempfile.mkdtemp())
        for n in ("V10__b.sql", "V2__a.sql", "V1__x.sql", "README.md"):
            write(d, n, "")
        self.assertEqual([p.name for p in dc.migration_files(d)], ["V1__x.sql", "V2__a.sql", "V10__b.sql"])


@unittest.skipUnless(docker_ok(), "docker 없음")
class DockerCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = rt.load_config()
        docker = containers.Docker.from_cfg(cfg)
        cls.stack = containers.PostgresStack(docker, cfg["db"], f"cx-test-{uuid.uuid4().hex[:8]}", internal=True)
        cls.stack.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        assert not cls.stack.cleanup_errors, cls.stack.cleanup_errors

    def test_full_schema_passes_and_extra_unique_fails(self):
        d = Path(tempfile.mkdtemp())
        write(d, "V1__init.sql", s0_sql())
        write(d, "V2__indexes.sql", V2)
        write(d, "V3__rest.sql", V3)
        collect = lambda m: dc.collect(m, self.stack)  # noqa: E731 — 호출마다 새 데이터베이스
        r = dc.check(collect(d))
        self.assertEqual({k: v["result"] for k, v in r.items()},
                         {k: "pass" for k in ("D0-01", "D1-01", "D1-02", "D1-04", "D4-01", "D6-01", "D7-02")}, r)
        write(d, "V4__bad.sql", "ALTER TABLE product ADD CONSTRAINT uq_product_name UNIQUE (name);\n"
                                "SELECT broken syntax here;\n")
        cat = collect(d)
        self.assertEqual(cat["migration_error"]["file"], "V4__bad.sql")
        self.assertEqual(dc.check(cat)["D1-04"]["result"], "pass")  # 실패한 파일은 한 트랜잭션으로 롤백
        write(d, "V4__bad.sql", "ALTER TABLE product ADD CONSTRAINT uq_product_name UNIQUE (name);\n")
        r = dc.check(collect(d))
        self.assertEqual((r["D1-04"]["result"], r["D1-04"]["extra_unique"]), ("fail", ["uq_product_name"]))


if __name__ == "__main__":
    unittest.main()
