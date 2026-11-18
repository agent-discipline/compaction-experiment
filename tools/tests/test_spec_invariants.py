"""명세 기반(blind) 불변식 테스트 — task C-blind-1.

기대값의 출처는 구현이 아니라 합의 문서다:
  docs/plans/2026-11-18/compaction-retention/requirement-spec.md (§2 불변식)
  tasks/seat-reservation/constraints.md (K1~K8 확인식)
  tasks/seat-reservation/scoring-items.md · hidden/README.md · hidden/items.json
  tools/README.md
도구는 공개 표면(CLI · 훅 stdin/stdout · README에 이름이 나온 함수)으로만 몬다.

안전: 실제 Docker·유료 호출 없음. setUpModule이 PATH 맨 앞에 "부르면 기록하고 125로 끝나는" docker shim을 두고
DOCKER_HOST를 없는 소켓으로 돌린다. 실행기를 모는 테스트는 tests/fake_docker.py를 cfg["docker_bin"]으로 쓴다
(README "테스트" 절의 방식). tearDownModule은 shim이 불렸으면 실패한다.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TOOLS, write, write_jsonl, user, assistant, tool_result  # noqa: E402  (sys.path에 tools/ 추가)

ROOT = TOOLS.parent
TASK = ROOT / "tasks" / "seat-reservation"
SKELETON = TASK / "skeleton"
CFG_REAL = json.loads((TOOLS / "experiment.json").read_text(encoding="utf-8"))
FAKE_DOCKER = TOOLS / "tests" / "fake_docker.py"

_SAVED = {}
_SHIM_DIR = None


def setUpModule():
    global _SHIM_DIR
    _SHIM_DIR = Path(tempfile.mkdtemp(prefix="cx-blind-shim-"))
    shim = _SHIM_DIR / "docker"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{_SHIM_DIR}/calls.log"\nexit 125\n')
    shim.chmod(0o755)
    _SAVED["PATH"] = os.environ.get("PATH")
    _SAVED["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
    os.environ["PATH"] = f"{_SHIM_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["DOCKER_HOST"] = f"unix://{_SHIM_DIR}/no-such-docker.sock"
    if shutil.which("docker") != str(shim):
        raise RuntimeError(f"docker shim is not first on PATH: {shutil.which('docker')}")


def tearDownModule():
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    log = _SHIM_DIR / "calls.log"
    called = log.read_text() if log.exists() else ""
    shutil.rmtree(_SHIM_DIR, ignore_errors=True)
    if called:
        raise AssertionError(f"docker shim was called unexpectedly:\n{called}")


# ---------------------------------------------------------------- 공용 픽스처

T0 = 1_800_000_000          # 골격 커밋 시각
STEP_GAP = 1000             # 턴 k 시작 = T0 + STEP_GAP*(k+1)
STEPS = [f"S{k}" for k in range(9)]


def run(cmd, cwd=None, env=None, check=False, input=None, timeout=300):
    p = subprocess.run(cmd, cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise AssertionError(f"{cmd} → {p.returncode}\nSTDOUT:{p.stdout[-3000:]}\nSTDERR:{p.stderr[-3000:]}")
    return p


def git(cwd, *args, date=T0):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e.invalid", GIT_AUTHOR_DATE=f"@{date} +0000",
               GIT_COMMITTER_DATE=f"@{date} +0000", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return run(["git", *args], cwd=cwd, env=env, check=True).stdout.strip()


def turn_start(k):
    return T0 + STEP_GAP * (k + 1)


COMPLIANT_MAIN = {
    "src/main/kotlin/com/example/booking/seat/config/ClockConfig.kt": """package com.example.booking.seat.config

import java.time.Clock
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

@Configuration
class ClockConfig {
    @Bean
    fun clock(): Clock = Clock.systemUTC()
}
""",
    "src/main/kotlin/com/example/booking/seat/hold/HoldService.kt": """package com.example.booking.seat.hold

import java.time.Clock
import java.time.Instant

// 지금 시각은 Instant.now() 를 쓰지 않고 주입한 clock 에서 얻는다. SQL now() 도 쓰지 않는다.
class HoldService(private val clock: Clock) {
    fun now(): Instant = Instant.now(clock)
    val sql = "DELETE FROM seat_hold WHERE expires_at <= :now"
}
""",
}
COMPLIANT_TEST = {
    "src/test/kotlin/com/example/booking/seat/hold/HoldServiceTest.kt": """package com.example.booking.seat.hold

import org.junit.jupiter.api.Test

class HoldServiceTest {
    @Test
    fun `같은 좌석을 두 명이 잡으면 한 명만 성공한다`() {
    }

    @Test
    fun `만료된 홀드는 확정할 수 없다`() {
    }
}
""",
}
V1 = "CREATE TABLE product (id BIGSERIAL PRIMARY KEY, name VARCHAR(200) NOT NULL);\n"
V2 = "CREATE INDEX idx_seat_hold_seat_id ON seat_hold (seat_id);\n"


class Fixture:
    """임시 작업 저장소(골격 + 단계별 커밋) + 실행 기록 폴더(run.json · turns/NN/transcript.jsonl)."""

    def __init__(self, base):
        self.base = Path(base)
        self.work = self.base / "work"
        self.rdir = self.base / "run"
        shutil.copytree(SKELETON, self.work)
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "skeleton", date=T0)
        self.skeleton_commit = git(self.work, "rev-parse", "HEAD")
        self.turns = []              # run.json turns
        self.transcript = []         # 세션 기록 행
        self.boundaries = []
        self.condition = "control"

    # 파일 조작
    def put(self, files):
        for rel, text in files.items():
            write(self.work, rel, text)

    def rm(self, rel):
        (self.work / rel).unlink()

    def commit(self, msg, date):
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", msg, date=date)

    def end_turn(self, k, prose="요청하신 단계를 구현했고 테스트도 모두 통과했습니다. 다음 단계를 알려 주세요.",
                 tracked_diff=None):
        """턴 k의 세션 기록 행(사용자 + 어시스턴트)과 run.json turns[k]를 남긴다."""
        self.transcript.append(user(f"S{k} 단계입니다."))
        self.transcript.append(assistant(prose, mid=f"m{k}"))
        head = git(self.work, "rev-parse", "HEAD")
        self.turns.append({
            "step": f"S{k}", "index": k, "started_at": turn_start(k), "ended_at": turn_start(k) + STEP_GAP - 1,
            "started_iso": "", "wall_s": STEP_GAP - 1, "exit_code": 0, "timed_out": False,
            "result_parsed": True, "is_error": False, "subtype": "success",
            "git_head": head, "tracked_diff_sha256": tracked_diff or hashlib.sha256(b"").hexdigest(),
            "transcript_lines": len(self.transcript), "transcript_bad_lines": 0,
        })

    def standard(self, upto=8, overrides=None, skip_commit=(), msg=None):
        """S0..S{upto} 모두 준수하는 기본 진행. overrides[k] = (files 또는 callable) — 단계 k 커밋 전에 적용."""
        overrides = overrides or {}
        for k in range(upto + 1):
            if k == 0:
                self.put(COMPLIANT_MAIN)
                self.put(COMPLIANT_TEST)
                self.put({"src/main/resources/db/migration/V1__create_tables.sql": V1})
            if k == 1:
                self.put({"src/main/resources/db/migration/V2__add_indexes.sql": V2})
            if k in overrides:
                o = overrides[k]
                o(self) if callable(o) else self.put(o)
            if k not in skip_commit:
                m = (msg or {}).get(k, f"[S{k}] 단계 {k} 구현")
                self.commit(m, turn_start(k) + 10)
            self.end_turn(k)

    def write_run(self, **extra):
        self.rdir.mkdir(parents=True, exist_ok=True)
        n = len(self.turns)
        tdir = self.rdir / "turns" / f"{n - 1:02d}"
        tdir.mkdir(parents=True, exist_ok=True)
        write_jsonl(tdir / "transcript.jsonl", self.transcript)
        rj = {
            "run_id": "fixture-run", "model": "claude-sonnet-5-5", "condition": self.condition, "rep": 1,
            "status": "complete", "measurement_complete": True, "abort_reason": None, "failure_reasons": [],
            "invalid": False, "invalid_reasons": [], "warnings": [], "skeleton_commit": self.skeleton_commit,
            "steps_planned": STEPS[:n], "turns": self.turns, "boundaries": self.boundaries,
            "injections": [], "compact_log": [], "totals": {"tokens": None, "cost_usd": None, "wall_s": 0},
            "session_id": "11111111-1111-1111-1111-111111111111",
        }
        rj.update(extra)
        (self.rdir / "run.json").write_text(json.dumps(rj, ensure_ascii=False), encoding="utf-8")
        return self.rdir


def score_config(base):
    """채점용 설정 사본 — 상대 경로를 절대 경로로 바꾸고 docker_bin을 '부르면 실패하는' 경로로."""
    cfg = json.loads(json.dumps(CFG_REAL))
    for k in ("task_dir", "skeleton_dir"):
        cfg[k] = str(ROOT / cfg[k])
    cfg["hidden"]["dir"] = str(ROOT / cfg["hidden"]["dir"])
    cfg["hidden"]["items_file"] = str(ROOT / cfg["hidden"]["items_file"])
    cfg["runs_dir"] = str(Path(base) / "runs")
    cfg["run_root"] = str(Path(base) / "cx-run")
    p = Path(base) / "experiment.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


def score(fx, **run_extra):
    """score_run.py CLI(--skip-hidden --skip-db)로 채점 → score.json."""
    rdir = fx.write_run(**run_extra)
    cfgp = score_config(fx.base)
    out = fx.base / "score.json"
    run([sys.executable, str(TOOLS / "score_run.py"), "--run", str(rdir), "--work", str(fx.work), "--skip-hidden",
         "--skip-db", "--out", str(out), "--config", str(cfgp)], cwd=ROOT, check=True)
    return json.loads(out.read_text(encoding="utf-8"))


def item(sc, iid):
    return sc["items"][iid]


# ================================================================ 1. K1~K8 (constraints.md 확인식)

class K1PackageTest(unittest.TestCase):
    """constraints.md K1: src/{main,test}/{kotlin,java}의 .kt·.java package가 ^package com\\.example\\.booking\\.seat(\\.|$).
    예외: 골격 BookingApplication(main)과 골격 테스트 지원 파일, 숨긴 계약 테스트(경로 제외)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k1-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_compliant_with_skeleton_exceptions_passes(self):
        fx = Fixture(self.tmp)
        fx.standard()
        k1 = item(score(fx), "K1")
        self.assertEqual(k1["result"], "pass", k1)

    def test_sibling_package_and_java_and_seatx_are_counted(self):
        fx = Fixture(self.tmp)
        fx.standard(overrides={5: {
            "src/main/kotlin/com/example/booking/config/SchedulingConfig.kt":
                "package com.example.booking.config\n\nclass SchedulingConfig\n",
            "src/main/java/com/example/booking/web/Ping.java":
                "package com.example.booking.web;\n\npublic class Ping {}\n",
            "src/main/kotlin/com/example/booking/seatx/Other.kt": "package com.example.booking.seatx\n\nclass Other\n",
            "src/test/kotlin/com/example/booking/HelperTest.kt": "package com.example.booking\n\nclass HelperTest\n",
            # 하위 패키지는 자유 — 위반 아님
            "src/main/kotlin/com/example/booking/seat/api/deep/Api.kt": "package com.example.booking.seat.api.deep\n\nclass Api\n",
        }})
        k1 = item(score(fx), "K1")
        self.assertEqual(k1["result"], "fail", k1)
        self.assertEqual(len(k1["violations"]), 4, k1)

    def test_hidden_contract_files_are_excluded(self):
        fx = Fixture(self.tmp)
        fx.standard()
        fx.put({"src/test/kotlin/hidden/contract/X.kt": "package hidden.contract\n\nclass X\n"})
        k1 = item(score(fx), "K1")
        self.assertEqual(k1["result"], "pass", k1)


class K2MigrationImmutableTest(unittest.TestCase):
    """constraints.md K2: 마이그레이션 경로마다 A 한 번뿐(M·D·R 0) + 작업 트리 변경 0. .gitkeep 제외.
    scoring-items K2: 턴 끝 HEAD 사이의 변경도 본다(이력을 고쳐 써도 보이게)."""

    MIG = "src/main/resources/db/migration"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k2-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def k2(self, **kw):
        fx = Fixture(self.tmp)
        fx.standard(**kw)
        return fx

    def test_add_only_passes_even_when_gitkeep_deleted(self):
        fx = self.k2(overrides={0: lambda f: f.rm(f"{self.MIG}/.gitkeep"),
                                4: {f"{self.MIG}/V3__add_quota.sql": "CREATE TABLE user_hold_quota (cnt INT);\n"}})
        r = item(score(fx), "K2")
        self.assertEqual(r["result"], "pass", r)

    def test_modify_committed_migration_fails(self):
        fx = self.k2(overrides={4: {f"{self.MIG}/V1__create_tables.sql": V1 + "ALTER TABLE product ADD COLUMN x INT;\n"}})
        self.assertEqual(item(score(fx), "K2")["result"], "fail")

    def test_delete_committed_migration_fails(self):
        fx = self.k2(overrides={6: lambda f: f.rm(f"{self.MIG}/V2__add_indexes.sql")})
        self.assertEqual(item(score(fx), "K2")["result"], "fail")

    def test_rename_committed_migration_fails(self):
        def ren(f):
            git(f.work, "mv", f"{self.MIG}/V2__add_indexes.sql", f"{self.MIG}/V2__add_seat_indexes.sql")
        fx = self.k2(overrides={5: ren})
        sc = score(fx)
        self.assertEqual(item(sc, "K2")["result"], "fail", item(sc, "K2"))
        self.assertEqual(item(sc, "K3")["result"], "pass")  # 이름 형식은 여전히 맞다 — K2만의 위반

    def test_uncommitted_change_in_worktree_fails(self):
        fx = self.k2()
        write(fx.work, f"{self.MIG}/V1__create_tables.sql", V1 + "-- edited\nALTER TABLE product ADD COLUMN y INT;\n")
        self.assertEqual(item(score(fx), "K2")["result"], "fail")

    def test_rewritten_history_still_detected(self):
        """S4에서 V1을 고친 뒤 골격까지 soft reset → 로그에는 A만 남는다. 턴 끝 HEAD 사이 diff로 잡혀야 한다."""
        def rewrite(f):
            write(f.work, f"{self.MIG}/V1__create_tables.sql", V1 + "ALTER TABLE product ADD COLUMN z INT;\n")
            git(f.work, "reset", "-q", "--soft", f.skeleton_commit)
        fx = self.k2(overrides={4: rewrite})
        sc = score(fx)
        log = git(fx.work, "log", "--format=", "--name-status", "-M", "--", self.MIG)
        self.assertNotIn("M\t", log)  # 전제: 이력만 보면 수정이 보이지 않는다
        self.assertEqual(item(sc, "K2")["result"], "fail", item(sc, "K2"))
        self.assertTrue(sc["history"]["rewritten_turns"], sc["history"])


class K3MigrationNamesTest(unittest.TestCase):
    """constraints.md K3: ^V[1-9][0-9]*__[a-z0-9]+(_[a-z0-9]+)*\\.sql$ 이고 번호 = {1..N}. .gitkeep 제외."""

    MIG = "src/main/resources/db/migration"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k3-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def k3(self, files):
        fx = Fixture(self.tmp)
        fx.standard(overrides={3: files})
        return item(score(fx), "K3")

    def test_contiguous_snake_names_pass(self):
        self.assertEqual(self.k3({f"{self.MIG}/V3__add_quota_table.sql": "SELECT 1;\n"})["result"], "pass")

    def test_gap_fails(self):
        self.assertEqual(self.k3({f"{self.MIG}/V4__skip_three.sql": "SELECT 1;\n"})["result"], "fail")

    def test_sub_version_fails(self):
        self.assertEqual(self.k3({f"{self.MIG}/V2_1__fix.sql": "SELECT 1;\n"})["result"], "fail")

    def test_uppercase_description_fails(self):
        self.assertEqual(self.k3({f"{self.MIG}/V3__Add_Quota.sql": "SELECT 1;\n"})["result"], "fail")

    def test_leading_zero_fails(self):
        self.assertEqual(self.k3({f"{self.MIG}/V03__add_quota.sql": "SELECT 1;\n"})["result"], "fail")

    def test_non_sql_file_in_folder_fails(self):
        self.assertEqual(self.k3({f"{self.MIG}/notes.txt": "x\n"})["result"], "fail")


class K4DependenciesTest(unittest.TestCase):
    """constraints.md K4: dependencies{}·plugins{} 블록이 골격과 같다(공백·주석 무시 정규화)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k4-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = (SKELETON / "build.gradle.kts").read_text(encoding="utf-8")

    def k4(self, text):
        fx = Fixture(self.tmp)
        fx.standard(overrides={2: {"build.gradle.kts": text}})
        return item(score(fx), "K4")

    def test_whitespace_and_comments_only_pass(self):
        t = self.base.replace("dependencies {\n", "dependencies {\n    // 주석만 추가\n\n   /* 블록 주석 */\n")
        t = t.replace('    implementation("org.postgresql:postgresql")',
                      '        implementation(  "org.postgresql:postgresql"  )   ')
        t = t.replace('version = "0.0.1-SNAPSHOT"', 'version = "0.0.2"')  # 블록 밖 변경은 대상 아님
        self.assertNotEqual(t, self.base)
        self.assertEqual(self.k4(t)["result"], "pass")

    def test_added_test_dependency_fails(self):
        t = self.base.replace("dependencies {\n", 'dependencies {\n    testImplementation("io.mockk:mockk:1.13.13")\n')
        self.assertEqual(self.k4(t)["result"], "fail")

    def test_added_plugin_fails(self):
        t = self.base.replace("plugins {\n", 'plugins {\n    id("org.jlleitschuh.gradle.ktlint") version "12.1.1"\n')
        self.assertEqual(self.k4(t)["result"], "fail")


class K5TestNamesTest(unittest.TestCase):
    """constraints.md K5: @Test·@ParameterizedTest·@RepeatedTest fun 이름 = 백틱 + 한글 1자 이상. 위반 = 비율."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k5-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_compliant_passes_and_skeleton_contextloads_exempt(self):
        fx = Fixture(self.tmp)
        fx.standard()
        r = item(score(fx), "K5")
        self.assertEqual(r["result"], "pass", r)

    def test_non_korean_or_non_backtick_names_counted_as_ratio(self):
        fx = Fixture(self.tmp)
        fx.standard(overrides={6: {"src/test/kotlin/com/example/booking/seat/confirm/ConfirmTest.kt": """package com.example.booking.seat.confirm

import org.junit.jupiter.api.RepeatedTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.ValueSource

class ConfirmTest {
    @Test
    fun confirmReturns404WhenLost() {
    }

    @Test
    fun `returns 409 on unique violation`() {
    }

    @ParameterizedTest
    @ValueSource(ints = [1, 2])
    fun 한글이지만백틱없음(n: Int) {
    }

    @RepeatedTest(3)
    fun `동시에 확정하면 하나만 성공한다`() {
    }

    fun helperWithoutAnnotation() {
    }
}
"""}})
        fx.put({"src/test/kotlin/hidden/contract/HiddenTest.kt":
                "package hidden.contract\n\nimport org.junit.jupiter.api.Test\n\nclass HiddenTest {\n    @Test\n    fun englishName() {\n    }\n}\n"})
        r = item(score(fx), "K5")
        self.assertEqual(r["result"], "fail", r)
        # 대상: 기본 2개 + ConfirmTest 4개(어노테이션 없는 helper 제외, 숨긴 테스트·골격 contextLoads 제외) = 6, 위반 3
        self.assertEqual(r["total"], 6, r)
        self.assertEqual(r["violation_count"], 3, r)
        self.assertAlmostEqual(r["rate"], 3 / 6, places=3)


class K6ClockTest(unittest.TestCase):
    """constraints.md K6: 메인 코드(주석 제거)에서 인자 없는 *.now()·System.currentTimeMillis( 0건,
    Clock.system*( 은 @Bean Clock 정의 1곳만, 문자열 안 SQL now()·CURRENT_TIMESTAMP·*_timestamp(·LOCALTIMESTAMP 0건(대소문자 무시).
    Instant.now(clock)은 위반 아님, 마이그레이션 .sql은 대상 아님."""

    SVC = "src/main/kotlin/com/example/booking/seat/hold/Extra.kt"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k6-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def k6(self, files):
        fx = Fixture(self.tmp)
        fx.standard(overrides={5: files})
        return item(score(fx), "K6")

    def kt(self, body):
        return {self.SVC: "package com.example.booking.seat.hold\n\nimport java.time.*\n\nclass Extra(private val clock: Clock) {\n"
                + body + "\n}\n"}

    def test_baseline_bean_clock_and_now_with_clock_and_comments_pass(self):
        # 기본 픽스처: @Bean Clock.systemUTC() 1곳 + Instant.now(clock) + 주석 속 Instant.now()·now()
        r = self.k6({})
        self.assertEqual(r["result"], "pass", r)

    def test_other_now_with_argument_pass(self):
        r = self.k6(self.kt("    fun a() = LocalDateTime.now(clock)\n    fun b() = OffsetDateTime.now( clock )\n"))
        self.assertEqual(r["result"], "pass", r)

    def test_argless_now_calls_fail(self):
        for body in ("    fun a() = Instant.now()", "    fun a() = LocalDateTime . now ( )",
                     "    fun a() = ZonedDateTime.now()", "    fun a() = LocalDate.now()",
                     "    fun a() = System.currentTimeMillis()"):
            with self.subTest(body=body):
                self.assertEqual(self.k6(self.kt(body))["result"], "fail")
                shutil.rmtree(self.tmp)
                os.makedirs(self.tmp)

    def test_java_main_is_in_scope(self):
        r = self.k6({"src/main/java/com/example/booking/seat/J.java":
                     "package com.example.booking.seat;\n\npublic class J { long t() { return System.currentTimeMillis(); } }\n"})
        self.assertEqual(r["result"], "fail", r)

    def test_test_code_is_out_of_scope(self):
        r = self.k6({"src/test/kotlin/com/example/booking/seat/T.kt":
                     "package com.example.booking.seat\n\nval t = java.time.Instant.now()\n"})
        self.assertEqual(r["result"], "pass", r)

    def test_second_clock_creation_outside_bean_fails(self):
        r = self.k6(self.kt("    val c: Clock = Clock.systemDefaultZone()"))
        self.assertEqual(r["result"], "fail", r)

    def test_sql_now_in_string_fails_case_insensitive(self):
        for sql in ('"UPDATE seat_hold SET extended_at = now() WHERE id = :id"',
                    '"DELETE FROM seat_hold WHERE expires_at <= current_timestamp"',
                    '"SELECT statement_timestamp()"', '"SELECT LOCALTIMESTAMP"'):
            with self.subTest(sql=sql):
                self.assertEqual(self.k6(self.kt(f"    val q = {sql}"))["result"], "fail")
                shutil.rmtree(self.tmp)
                os.makedirs(self.tmp)

    def test_sql_word_in_comment_is_not_counted(self):
        r = self.k6(self.kt('    // "now()" 를 쓰지 않는다 /* CURRENT_TIMESTAMP */\n    val q = "SELECT 1"'))
        self.assertEqual(r["result"], "pass", r)

    def test_migration_sql_is_out_of_scope(self):
        r = self.k6({"src/main/resources/db/migration/V3__add_created_at.sql":
                     "ALTER TABLE product ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();\n"})
        self.assertEqual(r["result"], "pass", r)


class K7CommitsTest(unittest.TestCase):
    """constraints.md K7 / scoring-items K7a·K7b: 골격 이후 모든 커밋 첫 줄 ^\\[S[0-8]\\] ,
    단계 k 창에 [Sk] 커밋 1개 이상(S8은 그 턴에 추적 파일이 바뀐 경우만), 번호가 단계와 다른 커밋 수."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k7-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def sc(self, **kw):
        fx = Fixture(self.tmp)
        fx.standard(**kw)
        return score(fx)

    def test_compliant_passes(self):
        sc = self.sc()
        self.assertEqual(item(sc, "K7a")["result"], "pass")
        self.assertEqual(item(sc, "K7b")["result"], "pass")

    def test_missing_prefix_counted(self):
        sc = self.sc(msg={4: "S4 카운터 upsert 구현", 6: "[S6]조건부 확정"})  # 둘째는 공백 없음
        r = item(sc, "K7b")
        self.assertEqual(r["result"], "fail", r)
        self.assertEqual(r["bad_prefix"], 2, r)
        # K7a는 접두와 무관하게 센다
        self.assertEqual(item(sc, "K7a")["result"], "pass", item(sc, "K7a"))

    def test_missing_step_commit_counted(self):
        sc = self.sc(skip_commit={2})
        r = item(sc, "K7a")
        self.assertEqual(r["result"], "fail", r)
        self.assertEqual(len(r["steps_without_commit"]), 1, r)
        self.assertIn("2", json.dumps(r["steps_without_commit"]))
        self.assertAlmostEqual(r["rate"], 1 / 9, places=3)

    def test_wrong_step_number_counted(self):
        r = item(self.sc(msg={3: "[S5] 번호가 틀린 커밋"}), "K7b")
        self.assertEqual(r["number_mismatch"], 1, r)
        self.assertEqual(r["bad_prefix"], 0, r)

    def test_s8_without_tracked_change_needs_no_commit(self):
        r = item(self.sc(skip_commit={8}), "K7a")
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["steps_requiring_commit"], 8, r)

    def test_s8_with_tracked_change_but_no_commit_fails(self):
        fx = Fixture(self.tmp)
        fx.standard(upto=7)
        write(fx.work, "src/main/kotlin/com/example/booking/seat/hold/HoldService.kt",
              COMPLIANT_MAIN["src/main/kotlin/com/example/booking/seat/hold/HoldService.kt"] + "// 마무리 수정\n")
        fx.end_turn(8, tracked_diff=hashlib.sha256(b"changed").hexdigest())
        r = item(score(fx), "K7a")
        self.assertEqual(r["result"], "fail", r)
        self.assertIn("8", json.dumps(r["steps_without_commit"]))


class K8LanguageTest(unittest.TestCase):
    """constraints.md K8: 텍스트 블록에서 코드 블록·인라인 코드·인용(>)·URL·경로를 뺀 본문의 한글/(한글+라틴).
    턴별, 0.5 미만이면 위반, 본문 20자 미만 턴은 제외."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-k8-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def turns(self, prose_by_turn):
        fx = Fixture(self.tmp)
        fx.standard()
        # 세션 기록을 다시 쓴다: 턴마다 사용자 1행 + 어시스턴트 1행
        fx.transcript = []
        for k, t in enumerate(fx.turns):
            fx.transcript.append(user(f"S{k} 단계입니다."))
            fx.transcript.append(assistant(prose_by_turn.get(k, "요청하신 단계를 모두 구현했고 테스트도 통과했습니다."), mid=f"m{k}"))
            t["transcript_lines"] = len(fx.transcript)
        return score(fx)

    def test_exclusions_leave_only_korean_prose(self):
        text = ("이번 단계에서 선점 경로를 고쳤고 테스트를 모두 통과시켰습니다.\n\n"
                "```kotlin\nfun holdSeat(request: HoldRequest): HoldResponse = service.hold(request)\n```\n"
                "설정 키는 `seat.hold.strategy` 이고 값은 `pessimistic-nowait` 입니다.\n"
                "> Original requirement: the losing request must receive HTTP 409 immediately\n"
                "참고 문서는 https://docs.spring.io/spring-boot/reference/features/external-config.html 입니다.\n"
                "변경 파일은 src/main/kotlin/com/example/booking/seat/hold/HoldService.kt 와 HoldController.kt 입니다.")
        sc = self.turns({3: text})
        t3 = [t for t in sc["k8_turns"] if t["turn"] == 3][0]
        self.assertEqual(t3["latin"], 0, t3)
        self.assertEqual(t3["ratio"], 1.0, t3)
        self.assertFalse(t3["violation"])
        self.assertEqual(item(sc, "K8")["result"], "pass")

    def test_korean_path_removed_but_korean_slash_prose_kept(self):
        prose = "선점/확정 흐름을 다시 정리했고 나머지 테스트도 모두 통과했습니다"
        text = prose + " 메모는 문서/설계/결정사항.md 와 src/main/kotlin/Foo.kt 에 있습니다"
        sc = self.turns({2: text})
        t2 = [t for t in sc["k8_turns"] if t["turn"] == 2][0]
        import re
        expected_hangul = len(re.findall(r"[가-힣]", prose + " 메모는 와 에 있습니다"))
        self.assertEqual(t2["latin"], 0, t2)
        self.assertEqual(t2["hangul"], expected_hangul, t2)

    def test_english_turn_is_violation(self):
        sc = self.turns({5: "I implemented the sales cutoff and all tests are passing now. Next step please."})
        t5 = [t for t in sc["k8_turns"] if t["turn"] == 5][0]
        self.assertTrue(t5["violation"], t5)
        self.assertLess(t5["ratio"], 0.5)
        r = item(sc, "K8")
        self.assertEqual(r["result"], "fail", r)
        self.assertEqual(r["judged_turns"], 9, r)

    def test_short_turn_excluded(self):
        sc = self.turns({2: "Done.", 4: "OK, all green"})
        for k in (2, 4):
            t = [x for x in sc["k8_turns"] if x["turn"] == k][0]
            self.assertIsNone(t["ratio"], t)
            self.assertFalse(t["violation"], t)
        r = item(sc, "K8")
        self.assertEqual(r["result"], "pass", r)
        self.assertEqual(r["judged_turns"], 7, r)

    def test_ratio_value(self):
        # 한글 12자 + 라틴 12자 → 0.5 (기준 미만 아님)
        sc = self.turns({1: "가나다라마바사아자차카타 abcdefghijkl"})
        t1 = [t for t in sc["k8_turns"] if t["turn"] == 1][0]
        self.assertEqual((t1["hangul"], t1["latin"]), (12, 12), t1)
        self.assertAlmostEqual(t1["ratio"], 0.5)
        self.assertFalse(t1["violation"], t1)


# ================================================================ 전달 단계 · 컴팩트 거리 · 전제 · 설정

class DeliveryAndPremiseTest(unittest.TestCase):
    """명세 §2: 모든 채점 항목에 '전달 단계'와 '그 뒤 첫 컴팩트까지의 거리'. tools/README 좌표:
    before_prompt = turn − 0.5, mid_turn = turn, unknown·after_last_turn = 위치 불명(따로 셈)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-dl-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_delivery_distance(self):
        fx = Fixture(self.tmp)
        fx.standard()
        fx.condition = "compact"
        b = {"line": 1, "timestamp": "t", "trigger": "auto", "pre_tokens": 82000, "post_tokens": 9000,
             "has_summary": True, "compact_metadata": {}}
        fx.boundaries = [dict(b, turn=3, position="before_prompt"), dict(b, turn=6, position="unknown")]
        sc = score(fx)
        k1 = item(sc, "K1")
        self.assertEqual(k1["delivered_step"], 0)
        self.assertEqual(k1["compactions_after_delivery"], 1, k1)
        self.assertEqual(k1["turns_to_first_compaction"], 3, k1)  # ceil(2.5) − 0
        self.assertEqual(k1["compactions_unlocated"], 1, k1)
        d510 = item(sc, "D5-10")
        self.assertEqual(d510["delivered_step"], 5)
        self.assertEqual(d510["compactions_after_delivery"], 0, d510)
        self.assertIsNone(d510["turns_to_first_compaction"], d510)
        self.assertEqual(item(sc, "D3-01")["delivered_step"], 4)  # 'S3→S4' → 뒤 단계
        self.assertEqual(item(sc, "D1-04")["delivered_step"], 1)  # 'S1(…)' → 1
        self.assertEqual(sc["compactions"], 2)

    def test_drop_candidates_follow_scoring_items_column(self):
        fx = Fixture(self.tmp)
        fx.standard()
        sc = score(fx)
        drops = {"D0-12", "D2-04", "D2-05", "D4-07", "D5-04", "D6-07", "D6-08", "D7-08"}
        for iid, v in sc["items"].items():
            with self.subTest(iid=iid):
                self.assertEqual(bool(v["drop_candidate"]), iid in drops)
        for deleted in ("D4-09", "D5-07", "D6-09"):
            self.assertNotIn(deleted, sc["items"])

    def _reversal(self, s3_impl):
        fx = Fixture(self.tmp)
        yml = (SKELETON / "src/main/resources/application.yml").read_text(encoding="utf-8")

        def s3(f):
            if s3_impl:
                f.put({"src/main/kotlin/com/example/booking/seat/hold/Limit.kt":
                       'package com.example.booking.seat.hold\n\nval LOCK = "SELECT pg_try_advisory_xact_lock(:k)"\n',
                       "src/main/resources/application.yml": yml + "seat:\n  hold:\n    limit-strategy: advisory-try\n"})

        def s4(f):
            p = f.work / "src/main/kotlin/com/example/booking/seat/hold/Limit.kt"
            if p.exists():
                p.unlink()
            f.put({"src/main/resources/application.yml": yml + "seat:\n  hold:\n    limitStrategy: COUNTER_UPSERT_PRECHECK\n"})
        fx.standard(overrides={3: s3, 4: s4})
        return score(fx)

    def test_reversal_premise_met(self):
        sc = self._reversal(True)
        self.assertEqual(item(sc, "D3-00")["result"], "pass", item(sc, "D3-00"))
        self.assertEqual(item(sc, "D3-01")["result"], "pass", item(sc, "D3-01"))
        self.assertEqual(item(sc, "D3-02")["result"], "pass", item(sc, "D3-02"))

    def test_reversal_premise_unmet_is_counted_separately(self):
        sc = self._reversal(False)
        self.assertEqual(item(sc, "D3-00")["result"], "fail")
        for iid in ("D3-01", "D3-02"):
            r = item(sc, iid)
            self.assertEqual(r["result"], "premise_unmet", r)
            self.assertEqual(r["raw_result"], "pass", r)

    def test_premise_unscorable_when_step_turn_has_no_commit(self):
        fx = Fixture(self.tmp)
        fx.standard(skip_commit={3})
        r = item(score(fx), "D3-00")
        self.assertEqual(r["result"], "unscorable", r)


class ConfigCommonRuleTest(unittest.TestCase):
    """scoring-items 공통 규칙: relaxed binding · Duration 동치(접미 없는 정수 = ms) · 프로필 파일은 보지 않음."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-blind-cf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def d010(self, files):
        fx = Fixture(self.tmp)
        fx.standard(overrides={0: files})
        return item(score(fx), "D0-10")

    def test_equivalent_spellings_pass(self):
        yml = (SKELETON / "src/main/resources/application.yml").read_text(encoding="utf-8")
        r = self.d010({"src/main/resources/application.yml":
                       yml + "seat:\n  hold:\n    ttl: PT5M\n    maxPerUser: 2\n    expiry_interval: 10000\n"})
        self.assertEqual(r["result"], "pass", r)

    def test_profile_file_is_ignored(self):
        yml = (SKELETON / "src/main/resources/application.yml").read_text(encoding="utf-8")
        r = self.d010({"src/main/resources/application.yml": yml + "seat:\n  hold:\n    ttl: 5\n    max-per-user: 2\n    expiry-interval: 10s\n",
                       "src/main/resources/application-prod.yml": "seat:\n  hold:\n    ttl: 5m\n"})
        self.assertEqual(r["result"], "fail", r)  # ttl 5 = 5ms


# ================================================================ 4. 숨긴 테스트 → 항목 대응 (items.json)

JUNIT_TMPL = ('<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="{cls}" tests="{n}">\n{cases}\n</testsuite>\n')


def junit_case(cls, name, child=""):
    return f'<testcase name="{name}" classname="{cls}" time="0.1">{child}</testcase>'


class HiddenMappingTest(unittest.TestCase):
    """hidden/README.md · items.json: class + display_name으로 testcase 대응. failure/error = fail(메시지 ^UNSCORABLE이면
    unscorable), skipped = unscorable, 대응 없음 = not_run, no_test = unscorable, alias_of(D6-04)는 D0-13과 같은 테스트.
    README: 메인 컴파일 실패 = 전부 fail, 메인은 되는데 testcase 0개 = 전부 unscorable."""

    ITEMS = json.loads((TASK / "hidden" / "items.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cx-blind-hd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        import score_run  # noqa: WPS433
        self.score_run = score_run
        self.cfg = json.loads(score_config(self.tmp).read_text(encoding="utf-8"))

    def write_xml(self, d, outcomes):
        """outcomes: id → child xml ('' = pass). items.json의 class·display_name으로 XML을 만든다."""
        by_cls = {}
        for it in self.ITEMS["items"]:
            if it["id"] in outcomes and it.get("class"):
                by_cls.setdefault(it["class"], []).append(junit_case(it["class"], it["display_name"], outcomes[it["id"]]))
        os.makedirs(d, exist_ok=True)
        for cls, cases in by_cls.items():
            (Path(d) / f"TEST-{cls}.xml").write_text(JUNIT_TMPL.format(cls=cls, n=len(cases), cases="\n".join(cases)))

    def hidden(self, main_rc, test_rc, outcomes, main_out=b"", test_out=b""):
        work = Path(tempfile.mkdtemp(dir=self.tmp)) / "work"
        shutil.copytree(SKELETON, work)
        seen = []

        def fake_executor(cfg, docker, stack):
            def ex(cmd, cwd, log, timeout):
                seen.append((list(cmd), sorted(str(p.relative_to(cwd)) for p in Path(cwd, "src", "test").rglob("*.kt"))
                             if Path(cwd, "src", "test").exists() else []))
                if "test" in cmd and outcomes is not None:
                    self.write_xml(Path(cwd) / self.cfg["hidden"]["junit_dir"], outcomes)
                out = main_out if "classes" in cmd else test_out
                if out:
                    log.write(out)
                    log.flush()
                return main_rc if "classes" in cmd else test_rc
            return ex

        orig = self.score_run.container_executor
        self.score_run.container_executor = fake_executor
        try:
            res = self.score_run.hidden_items(self.cfg, str(work), str(self.tmp), False, docker=None, stack=object())
        finally:
            self.score_run.container_executor = orig
        items = res[0] if isinstance(res, tuple) else res
        return items, seen

    def test_outcomes_map_by_class_and_display_name(self):
        outcomes = {
            "D0-02": "",
            "D0-03": '<failure message="expected 409">x</failure>',
            "D0-05": '<error message="boom">x</error>',
            "D0-04": '<skipped message="UNSCORABLE seed failed [sqlstate=23502]"/>',
            "D2-01": '<failure message="UNSCORABLE wait not observed">x</failure>',
            "D0-07": '<failure message="SEED_SCHEMA_MISSING [sqlstate=42P01]">x</failure>',
            "D0-13": "",
        }
        items, seen = self.hidden(0, 1, outcomes)
        self.assertEqual(items["D0-02"]["result"], "pass")
        self.assertEqual(items["D0-03"]["result"], "fail")
        self.assertEqual(items["D0-05"]["result"], "fail")
        self.assertEqual(items["D0-04"]["result"], "unscorable")
        self.assertEqual(items["D2-01"]["result"], "unscorable")
        self.assertEqual(items["D0-07"]["result"], "fail")
        self.assertEqual(items["D0-06"]["result"], "not_run")
        self.assertEqual(items["D0-12"]["result"], "unscorable")      # no_test
        self.assertEqual(items["D6-04"]["result"], "pass")            # D0-13과 같은 testcase
        self.assertEqual(items["D6-04"]["alias_of"], "D0-13")
        ids = {it["id"] for it in self.ITEMS["items"]}
        self.assertTrue(ids <= set(items), ids - set(items))
        # 순서: 에이전트 src/test 삭제 → 메인만 컴파일 → 숨긴 테스트 덮어 실행
        self.assertIn("classes", seen[0][0])
        self.assertEqual(seen[0][1], [])
        self.assertIn("test", seen[1][0])
        self.assertTrue(seen[1][1] and all(p.startswith("src/test/kotlin/hidden/contract/") for p in seen[1][1]), seen[1][1])

    COMPILE_ERR = (b"e: file:///work/src/main/kotlin/com/example/booking/seat/A.kt:3:5 Unresolved reference 'x'.\n"
                   b"FAILURE: Build failed with an exception.\n* What went wrong:\n"
                   b"Execution failed for task ':compileKotlin'.\n> Compilation error. See log for more details\n")
    ENV_ERR = (b"FAILURE: Build failed with an exception.\n* What went wrong:\n"
               b"Execution failed for task ':compileKotlin'.\n> Could not resolve all files for configuration "
               b"':compileClasspath'.\n   > Could not resolve io.mockk:mockk:1.13.13.\n")

    def assert_all(self, items, expected):
        for it in self.ITEMS["items"]:
            with self.subTest(id=it["id"]):
                self.assertEqual(items[it["id"]]["result"], "unscorable" if it.get("no_test") else expected)

    def test_main_compile_error_fails_every_hidden_item(self):
        """scoring-items '숨긴 테스트 빌드 실패': 메인 컴파일이 코드 원인(컴파일 오류 표지) = 전부 fail."""
        items, seen = self.hidden(1, 0, None, main_out=self.COMPILE_ERR)
        self.assertEqual(len(seen), 1)  # 숨긴 테스트는 돌지 않음
        self.assert_all(items, "fail")

    def test_main_build_env_failure_is_unscorable(self):
        """환경 표지(의존성 받기)는 컴파일 표지보다 앞선다 → 전부 unscorable."""
        items, _ = self.hidden(1, 0, None, main_out=self.ENV_ERR)
        self.assert_all(items, "unscorable")

    def test_main_build_docker_start_failure_is_unscorable(self):
        for rc in (125, 126, 127):
            with self.subTest(rc=rc):
                items, _ = self.hidden(rc, 0, None, main_out=b"docker: Error response from daemon\n")
                self.assertEqual(items["D0-02"]["result"], "unscorable")

    def test_main_build_unclassified_failure_is_unscorable(self):
        items, _ = self.hidden(1, 0, None, main_out=b"something odd happened\n")
        self.assert_all(items, "unscorable")

    def test_hidden_build_failure_with_no_testcases_is_unscorable(self):
        items, _ = self.hidden(0, 1, None, test_out=b"e: file:///work/src/test/kotlin/hidden/contract/X.kt:1:1 "
                                                      b"Unresolved reference 'BookingApplication'.\n")
        self.assert_all(items, "unscorable")


# ================================================================ 2. 아웃박스 훅 (직접 — stdin/stdout 계약)

OUTBOX = TOOLS / "outbox"


def hook(script, args, payload, cwd):
    return run([sys.executable, "-I", str(script), *map(str, args)], cwd=cwd, input=json.dumps(payload, ensure_ascii=False))


def parse_ts(ts):
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class OutboxHookTest(unittest.TestCase):
    """명세 §1·§2 + tools/README '아웃박스 훅': UserPromptSubmit이 받은 메시지를 발생 시점에 파일에 덧붙이고,
    컴팩트 뒤 SessionStart(compact)가 그 파일만 읽어 최근 N개를 메시지당 L자로 잘라 rule 뒤에 주입, 주입문 해시 기록."""

    SID = "sess-blind-1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cx-blind-ob-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "hookdata"
        self.data.mkdir()

    def capture(self, prompt, sid=None):
        before = time.time()
        p = hook(OUTBOX / "capture.py", [self.data], {"session_id": sid or self.SID, "prompt": prompt,
                                                       "hook_event_name": "UserPromptSubmit"}, self.tmp)
        after = time.time()
        return p, before, after

    def rows(self):
        f = self.data / "outbox" / f"{self.SID}.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_capture_records_each_prompt_at_send_time_with_no_stdout(self):
        prompts = ["첫 메시지 — 제약 8개", "S1 단계입니다.\n인덱스 5개", "S2 단계입니다."]
        windows = []
        for t in prompts:
            p, b, a = self.capture(t)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "")   # UserPromptSubmit stdout은 맥락에 들어간다 — 아무것도 쓰지 않음
            windows.append((b, a))
            time.sleep(0.01)
        rows = self.rows()
        self.assertEqual([r["prompt"] for r in rows], prompts)
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        for r, (b, a) in zip(rows, windows):
            self.assertLessEqual(b - 1, parse_ts(r["ts"]))
            self.assertLessEqual(parse_ts(r["ts"]), a + 1)

    def inject(self, cfg, source="compact"):
        cfgp = self.tmp / "outbox-config.json"
        cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return hook(OUTBOX / "inject.py", [self.data, cfgp], {"session_id": self.SID, "source": source,
                                                               "hook_event_name": "SessionStart"}, self.tmp)

    def test_inject_recent_n_with_length_cap_and_hash(self):
        prompts = ["메시지 하나", "메시지 둘", "메시지 셋", "가" * 25]
        for t in prompts:
            self.capture(t)
        rule = "규칙: 아래 원문이 요약보다 우선합니다."
        p = self.inject({"recent_messages": 2, "max_chars_per_message": 10, "rule": rule})
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        rows = self.rows()
        expected = "\n\n".join([rule] + [
            f"[사용자 메시지 {rows[2]['seq']} · {rows[2]['ts']}]\n메시지 셋",
            f"[사용자 메시지 {rows[3]['seq']} · {rows[3]['ts']}]\n" + "가" * 10 + "\n…(이하 15자 생략)",
        ])
        self.assertEqual(ctx, expected)
        self.assertNotIn("메시지 하나", ctx)
        log = [json.loads(x) for x in (self.data / "inject-log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(log), 1)
        e = log[0]
        self.assertEqual(e["seqs"], [3, 4])
        self.assertEqual(e["recent_messages"], 2)
        self.assertEqual(e["max_chars_per_message"], 10)
        self.assertEqual(e["clipped_messages"], 1)
        self.assertEqual(e["chars_injected"], len(ctx))
        self.assertEqual(e["context_sha256"], hashlib.sha256(ctx.encode("utf-8")).hexdigest())
        self.assertEqual(e["rule_sha256"], hashlib.sha256(rule.encode("utf-8")).hexdigest())

    def test_default_config_reinjects_all_task_messages_unclipped(self):
        """README: 과제 메시지 9개 · 가장 긴 첫 메시지 5,188자 → 기본값(10, 8000)이면 전부 잘리지 않고 들어간다."""
        cfg = json.loads((OUTBOX / "config.json").read_text(encoding="utf-8"))
        self.assertEqual((cfg["recent_messages"], cfg["max_chars_per_message"]), (10, 8000))
        cons = (TASK / "constraints.md").read_text(encoding="utf-8")
        block = cons.split("## 보낼 문장", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
        msgs = [block + "\n\n" + (TASK / "steps" / "S0.md").read_text(encoding="utf-8").rstrip("\n")] + [
            (TASK / "steps" / f"S{k}.md").read_text(encoding="utf-8").rstrip("\n") for k in range(1, 9)]
        for m in msgs:
            self.capture(m)
        p = self.inject(cfg)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        for m in msgs:
            self.assertIn(m, ctx)
        self.assertNotIn("생략)", ctx)

    def test_no_injection_without_outbox_file_or_on_bad_input(self):
        p = self.inject({"recent_messages": 2, "max_chars_per_message": 10, "rule": "r"})
        self.assertEqual(p.returncode, 0)
        out = p.stdout.strip()
        if out:   # 메시지가 없을 때 빈 주입을 낼 수는 있어도 사용자 메시지 블록은 없어야 한다
            self.assertNotIn("[사용자 메시지", out)
        p2, _, _ = self.capture("x", sid="../../escape")
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p2.stdout, "")
        self.assertFalse((self.tmp / "escape.jsonl").exists())
        self.assertTrue((self.data / "hook-errors.jsonl").exists())


class CompactLogHookTest(unittest.TestCase):
    """compact_log.py: PreCompact·PostCompact 입력을 남기고 stdout에 아무것도 쓰지 않는다(요약 지시문 오염 금지)."""

    def test_records_trigger_and_prints_nothing(self):
        tmp = Path(tempfile.mkdtemp(prefix="cx-blind-cl-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for ev, extra in (("PreCompact", {}), ("PostCompact", {"compact_summary": "요약문"})):
            p = hook(TOOLS / "compact_log.py", [tmp], dict({"session_id": "s1", "hook_event_name": ev, "trigger": "auto"},
                                                             **extra), tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "")
        rows = [json.loads(x) for x in (tmp / "compact-log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["event"] for r in rows], ["PreCompact", "PostCompact"])
        self.assertTrue(all(r["trigger"] == "auto" for r in rows))


# ================================================================ 2·3·5. 실행기 전체 흐름 (가짜 docker + 가짜 claude)

TOKEN = "sk-ant-oat01-BLINDFAKETOKEN-7f3a9c0e1d2b"


def run_task(base, condition, last_step, env_extra=None, cfg_over=None, model="claude-haiku-5-5"):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    cfgp = score_config(base)
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["docker_bin"] = str(FAKE_DOCKER)
    tok = base / "claude.env"
    tok.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    tok.chmod(0o600)
    cfg["token_env_file"] = str(tok)
    for k, v in (cfg_over or {}).items():
        cfg[k] = v
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, FAKE_DOCKER_STATE=str(base / "dstate"), **(env_extra or {}))
    p = run([sys.executable, str(TOOLS / "run_task.py"), "--model", model, "--condition", condition, "--rep", "1",
             "--last-step", str(last_step), "--config", str(cfgp)], cwd=ROOT, env=env)
    runs = sorted((base / "runs").glob("*")) if (base / "runs").exists() else []
    calls_f = base / "dstate" / "calls.jsonl"
    calls = [json.loads(x) for x in calls_f.read_text().splitlines()] if calls_f.exists() else []
    rj = json.loads((runs[0] / "run.json").read_text(encoding="utf-8")) if runs else None
    return {"proc": p, "rdir": runs[0] if runs else None, "run": rj, "calls": calls, "base": base, "cfg": cfg}


def all_record_bytes(rdir):
    """기록 폴더의 모든 파일 내용(압축본은 풀어서) — (경로, bytes)."""
    import tarfile
    for f in Path(rdir).rglob("*"):
        if not f.is_file():
            continue
        yield str(f), f.name.encode() + f.read_bytes()
        if f.name.endswith(".tar.gz"):
            with tarfile.open(f) as t:
                for m in t.getmembers():
                    if m.isfile():
                        yield f"{f}!{m.name}", m.name.encode() + t.extractfile(m).read()


class RunnerFlowTest(unittest.TestCase):
    """명세 §2: 조건 간에 달라지는 것은 컴팩트 기준값 env와 훅 설정뿐 · 경계 기록 · 대조군 경계 = 무효 · 컴팩트 조건 경계 없음 = 무효.
    아웃박스는 outbox 조건에만, 주입 해시 기록. tools/README 안전: 리소스 이름 접두 cx-, payment- 불가침, 토큰은 경로로만."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cx-blind-rt-"))
        cls.r = {}
        for cond in ("control", "compact", "outbox"):
            cls.r[cond] = run_task(cls.tmp / cond, cond, 2)
        cls.r["compact_no_boundary"] = run_task(cls.tmp / "nb", "compact", 0)
        cls.r["control_with_boundary"] = run_task(
            cls.tmp / "cb", "control", 1,
            cfg_over={"compact_window": {"control": "100000", "compact": "100000", "outbox": "100000"}})
        cls.r["leak"] = run_task(cls.tmp / "leak", "compact", 1, env_extra={"FAKE_LEAK_TURN": "1"})
        cls.r["audit"] = run_task(cls.tmp / "audit", "control", 0, env_extra={
            "FAKE_TOOL_CMD": f"cat {TASK}/constraints.md"})
        cls.r["payment"] = run_task(cls.tmp / "pay", "control", 0, env_extra={"FAKE_DOCKER_PS": "payment-lab-run-1"})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ok(self, key):
        r = self.r[key]
        self.assertIsNotNone(r["run"], r["proc"].stderr[-3000:])
        return r

    def test_runs_complete(self):
        for cond in ("control", "compact", "outbox"):
            with self.subTest(cond=cond):
                r = self.ok(cond)
                self.assertEqual(r["proc"].returncode, 0, r["proc"].stderr[-2000:])
                self.assertTrue(r["run"]["measurement_complete"])
                self.assertFalse(r["run"]["invalid"], r["run"]["invalid_reasons"])

    def test_conditions_differ_only_in_window_env_and_hook_settings(self):
        runs = {c: self.ok(c)["run"] for c in ("control", "compact", "outbox")}
        msgs = {c: r["messages_sha256_all"] for c, r in runs.items()}
        self.assertEqual(len(set(msgs.values())), 1, msgs)
        envs = {c: {k: v for k, v in r["container_env"].items() if k != "CLAUDE_CODE_AUTO_COMPACT_WINDOW"}
                for c, r in runs.items()}
        self.assertEqual(envs["control"], envs["compact"])
        self.assertEqual(envs["control"], envs["outbox"])
        self.assertEqual(runs["control"]["container_env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "1000000")
        self.assertEqual(runs["compact"]["container_env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "100000")
        for k in ("skeleton_commit", "container_limits", "image_id", "cli_version"):
            self.assertEqual(runs["control"][k], runs["compact"][k], k)
            self.assertEqual(runs["control"][k], runs["outbox"][k], k)
        self.assertEqual(runs["control"]["settings_sha256"], runs["compact"]["settings_sha256"])
        self.assertNotEqual(runs["control"]["settings_sha256"], runs["outbox"]["settings_sha256"])
        for c in runs:
            st = (self.r[c]["rdir"] / "settings.json").read_bytes()
            self.assertEqual(hashlib.sha256(st).hexdigest(), runs[c]["settings_sha256"], c)

    def test_messages_are_task_messages_verbatim(self):
        r = self.ok("control")
        msgs = json.loads((r["rdir"] / "messages.json").read_text(encoding="utf-8"))
        cons = (TASK / "constraints.md").read_text(encoding="utf-8")
        block = cons.split("## 보낼 문장", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(msgs[0]["text"], block + "\n\n" + (TASK / "steps/S0.md").read_text(encoding="utf-8").rstrip("\n"))
        self.assertEqual(msgs[1]["text"], (TASK / "steps/S1.md").read_text(encoding="utf-8").rstrip("\n"))
        for m in msgs:
            self.assertEqual(m["sha256"], hashlib.sha256(m["text"].encode("utf-8")).hexdigest())

    def test_outbox_hooks_only_in_outbox_condition(self):
        for c in ("control", "compact"):
            with self.subTest(cond=c):
                r = self.ok(c)
                st = json.loads((r["rdir"] / "settings.json").read_text(encoding="utf-8"))
                self.assertNotIn("UserPromptSubmit", st.get("hooks", {}))
                self.assertNotIn("SessionStart", st.get("hooks", {}))
                self.assertEqual(r["run"]["injections"], [])
                self.assertFalse(list((r["rdir"] / "hookdata").rglob("inject-log.jsonl")))
                self.assertFalse(list((r["rdir"] / "hookdata").rglob("outbox/*.jsonl")))
        st = json.loads((self.r["outbox"]["rdir"] / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", st["hooks"])
        self.assertEqual(st["hooks"]["SessionStart"][0].get("matcher"), "compact")

    def test_compact_condition_has_boundaries_but_no_injection(self):
        r = self.ok("compact")["run"]
        self.assertGreaterEqual(len(r["boundaries"]), 1)
        self.assertEqual(r["injections"], [])

    def test_boundaries_record_trigger_tokens_and_turn(self):
        r = self.ok("outbox")["run"]
        bs = r["boundaries"]
        self.assertEqual(len(bs), 2)   # 가짜 claude: 두 번째 턴부터 매 턴 프롬프트 앞 컴팩트
        for b, turn in zip(bs, (1, 2)):
            self.assertEqual(b["trigger"], "auto")
            self.assertEqual((b["pre_tokens"], b["post_tokens"]), (82000, 9000))
            self.assertEqual(b["turn"], turn)
            self.assertEqual(b["position"], "before_prompt")
        # 턴별 사본에도 그 시점까지의 경계
        tb = json.loads((self.r["outbox"]["rdir"] / "turns" / "01" / "boundaries.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tb), 1)

    def test_outbox_injection_per_boundary_with_hash_of_reconstructed_context(self):
        r = self.ok("outbox")
        run_ = r["run"]
        inj = run_["injections"]
        self.assertEqual(len(inj), len(run_["boundaries"]))
        rows = []
        for f in (r["rdir"] / "hookdata" / "outbox").glob("*.jsonl"):
            rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        msgs = json.loads((r["rdir"] / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual([x["prompt"] for x in rows], [m["text"] for m in msgs])   # 보낸 그대로 기록
        cfg = run_["outbox_config"]
        by_seq = {x["seq"]: x for x in rows}
        for e in inj:
            self.assertEqual(e["recent_messages"], cfg["recent_messages"])
            self.assertEqual(e["max_chars_per_message"], cfg["max_chars_per_message"])
            ctx = "\n\n".join([cfg["rule"]] + [f"[사용자 메시지 {s} · {by_seq[s]['ts']}]\n{by_seq[s]['prompt']}"
                                               for s in e["seqs"]])
            self.assertEqual(e["context_sha256"], hashlib.sha256(ctx.encode("utf-8")).hexdigest())
        # 주입 시점의 최근 메시지 = 그때까지 기록된 사용자 메시지(컴팩트가 프롬프트 앞이면 이번 프롬프트 전까지)
        self.assertEqual(inj[0]["seqs"], [1])
        self.assertEqual(inj[1]["seqs"], [1, 2])

    def test_capture_times_fall_in_turn_windows(self):
        r = self.ok("outbox")
        rows = []
        for f in (r["rdir"] / "hookdata" / "outbox").glob("*.jsonl"):
            rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        for row, t in zip(rows, r["run"]["turns"]):
            self.assertLessEqual(t["started_at"] - 1, parse_ts(row["ts"]))
            self.assertLessEqual(parse_ts(row["ts"]), t["ended_at"] + 1)

    def test_compact_condition_without_boundary_is_invalid(self):
        r = self.ok("compact_no_boundary")["run"]
        self.assertEqual(r["boundaries"], [])
        self.assertTrue(r["invalid"])
        self.assertIn("no_compaction", r["invalid_reasons"])
        self.assertTrue((self.r["compact_no_boundary"]["rdir"] / "run.json").exists())  # 기록은 남는다

    def test_control_with_boundary_is_invalid(self):
        r = self.ok("control_with_boundary")["run"]
        self.assertGreaterEqual(len(r["boundaries"]), 1)
        self.assertTrue(r["invalid"])
        self.assertIn("control_has_compaction", r["invalid_reasons"])

    def test_reading_task_folder_makes_run_invalid(self):
        """명세 §2: 이 저장소의 과제 폴더를 읽으려 한 흔적(도구 입력의 경로)이 있으면 그 실행은 무효."""
        r = self.ok("audit")["run"]
        self.assertTrue(r["invalid"], r["invalid_reasons"])
        self.assertTrue(any(x.startswith("audit:") for x in r["invalid_reasons"]), r["invalid_reasons"])

    def _names_touched(self, calls):
        created, removed = [], []
        for c in calls:
            if c[0] in ("create", "run") and "--name" in c:
                created.append(c[c.index("--name") + 1])
            if c[:2] in (["network", "create"], ["volume", "create"]):
                created.append(c[-1])
            if c[0] in ("rm", "kill", "stop"):
                removed += [x for x in c[1:] if not x.startswith("-")]
            if c[:2] in (["network", "rm"], ["volume", "rm"]):
                removed += [x for x in c[2:] if not x.startswith("-")]
        return created, removed

    def test_resource_names_carry_prefix_and_label_and_never_touch_payment(self):
        for key in ("control", "compact", "outbox", "leak"):
            with self.subTest(run=key):
                calls = self.ok(key)["calls"]
                created, removed = self._names_touched(calls)
                self.assertTrue(created)
                for n in created + removed:
                    self.assertTrue(n.startswith("cx-"), n)
                    self.assertNotIn("payment-", n)
                for c in calls:
                    if c[0] in ("create", "run") or c[:2] in (["network", "create"], ["volume", "create"]):
                        self.assertIn("cx.experiment=compaction-experiment", c, c)
                    self.assertNotIn("prune", c)
                auto_removed = {c[c.index("--name") + 1] for c in calls if c[0] == "run" and "--rm" in c}
                self.assertLessEqual(set(created) - auto_removed, set(removed))   # 만든 것은 정확한 이름으로 정리

    def test_refuses_when_payment_container_running(self):
        r = self.r["payment"]
        self.assertNotEqual(r["proc"].returncode, 0)
        created, removed = self._names_touched(r["calls"])
        self.assertEqual(created, [])
        self.assertEqual(removed, [])
        self.assertFalse(Path(r["cfg"]["run_root"]).exists())

    def test_token_passed_by_path_only_and_absent_from_records(self):
        for key in ("control", "compact", "outbox"):
            with self.subTest(run=key):
                r = self.ok(key)
                self.assertEqual(r["run"]["token_env_file"], r["cfg"]["token_env_file"])
                for c in r["calls"]:
                    self.assertFalse(any(TOKEN in x for x in c), c)
                    if c[0] == "create" and "claude" in c:
                        self.assertIn("--env-file", c)
                        self.assertEqual(c[c.index("--env-file") + 1], r["cfg"]["token_env_file"])
                for path, data in all_record_bytes(r["rdir"]):
                    self.assertNotIn(TOKEN.encode(), data, path)
                self.assertNotIn(TOKEN, r["proc"].stdout + r["proc"].stderr)

    def test_token_leak_is_scrubbed_and_marks_run_failed(self):
        r = self.ok("leak")
        self.assertIn("secret_leak", r["run"]["failure_reasons"])
        self.assertEqual(r["run"]["status"], "failed")
        self.assertTrue((r["rdir"] / "SECRET-LEAK.txt").exists())
        for path, data in all_record_bytes(r["rdir"]):
            self.assertNotIn(TOKEN.encode(), data, path)
        # 운영 문제는 측정을 무효로 하지 않는다
        self.assertTrue(r["run"]["measurement_complete"])


class DockerGuardTest(unittest.TestCase):
    """containers.py / README: 지우기는 접두 cx-의 정확한 이름만, payment-는 접두와 무관하게 거부 — docker를 부르기 전에."""

    def test_remove_refuses_foreign_and_payment_names_without_calling_docker(self):
        import containers
        tmp = Path(tempfile.mkdtemp(prefix="cx-blind-dg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = tmp / "docker"
        rec.write_text(f'#!/bin/sh\necho "$@" >> "{tmp}/rec.log"\nexit 0\n')
        rec.chmod(0o755)
        d = containers.Docker(bin=str(rec), prefix="cx-")
        for kind in containers.KINDS:
            for name in ("payment-lab-run-1", "other-thing", "cx", "payment-cx-x"):
                with self.subTest(kind=kind, name=name):
                    with self.assertRaises(Exception):
                        d.remove(kind, name)
        with self.assertRaises(Exception):   # 보호 접두로 인스턴스를 만들 수도 없어야 한다
            containers.Docker(bin=str(rec), prefix="payment-").remove("container", "payment-lab-run-1")
        self.assertFalse((tmp / "rec.log").exists(), (tmp / "rec.log").read_text() if (tmp / "rec.log").exists() else "")


# ================================================================ 4. 집계 (summarize.py)

def mk_run(base, name, cond, results, comp_after, model="claude-sonnet-5-5", invalid=False, mc=True, score=True,
           **over):
    """results: id → result 또는 (result, {추가 키})."""
    d = Path(base) / "runs" / name
    d.mkdir(parents=True)
    window = "1000000" if cond == "control" else "100000"
    rj = dict(run_id=name, model=model, condition=cond, rep=1, status="complete" if mc else "aborted",
              measurement_complete=mc, invalid=invalid, invalid_reasons=["no_compaction"] if invalid else [],
              failure_reasons=[], settings_sha256=f"{cond}-settings", compact_window_env=window,
              outbox_config_sha256="ob", container_env={"HOME": "/home/agent", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": window},
              messages_sha256_all="msgs", image_id="sha256:img", skeleton_commit="skel", cli_version="2.1.326",
              limits={}, container_limits={"cpus": "4"}, turns=[], boundaries=[])
    rj.update(over)
    (d / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    run_sha = hashlib.sha256((d / "run.json").read_bytes()).hexdigest()
    if score:
        items = {}
        for iid, v in results.items():
            res, extra = (v if isinstance(v, tuple) else (v, {}))
            ca = extra.pop("compactions_after_delivery", comp_after)
            items[iid] = dict(dict(result=res, kind="M", delivered_step=0, drop_candidate=False,
                                   compactions_after_delivery=ca, compactions_unlocated=0,
                                   turns_to_first_compaction=1 if ca else None), **extra)
        sc = dict(run_id=name, model=model, condition=cond, rep=1, run_sha256=run_sha, status=rj["status"], invalid=invalid,
                  invalid_reasons=rj["invalid_reasons"], items=items, compactions=1 if comp_after else 0,
                  boundaries=[], k8_turns=[], history={"rewritten_turns": [], "missing_turns": []}, totals={})
        (d / "score.json").write_text(json.dumps(sc), encoding="utf-8")
    return d


def summarize(base):
    out = Path(base) / "summary.json"
    p = run([sys.executable, str(TOOLS / "summarize.py"), "--runs-dir", str(Path(base) / "runs"), "--json", str(out)],
            cwd=ROOT, check=True)
    return json.loads(out.read_text(encoding="utf-8")), p.stdout


class SummarizeTest(unittest.TestCase):
    """명세 §2 · README summarize: 주 측정값 = 컴팩트 실행의 '전달 뒤 컴팩트 항목' 위반율 − 같은 모델 대조군의 같은 항목 위반율.
    입력 불일치는 섞지 않고 따로 낸다. 무효·미채점·미완료는 조용히 빼지 않고 수와 목록을 낸다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cx-blind-sm-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def row(self, res, model, cond):
        return [r for r in res["rows"] if r["model"] == model and r["condition"] == cond][0]

    def test_paired_difference_uses_same_items_and_same_model_controls(self):
        b = self.tmp
        mk_run(b, "ctl1", "control", {"K1": "pass", "K2": "pass", "K3": "fail", "D5-10": "fail"}, 0)
        mk_run(b, "ctl2", "control", {"K1": "pass", "K2": "fail", "K3": "fail", "D5-10": "fail"}, 0)
        # 다른 모델의 대조군 — 짝에 쓰이면 안 된다
        mk_run(b, "ctl-opus", "control", {"K1": "fail", "K2": "fail", "K3": "fail", "D5-10": "pass"}, 0,
               model="claude-opus-5-5")
        # 컴팩트 실행: D5-10은 전달 뒤 컴팩트 없음 → 항목 집합에서 빠짐
        mk_run(b, "cmp1", "compact", {"K1": "fail", "K2": "fail", "K3": "pass",
                                      "D5-10": ("pass", {"compactions_after_delivery": 0})}, 1)
        res, _ = summarize(b)
        pairs = [p for p in res["pairs"] if p["run_id"] == "cmp1"]
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual(p["items"], 3)
        self.assertAlmostEqual(p["rate"], 2 / 3)
        self.assertEqual(p["controls"], 2)
        # 대조군 같은 항목 집합(K1~K3): ctl1 1/3, ctl2 2/3 → 평균 0.5
        self.assertAlmostEqual(p["control_rate_mean"], 0.5)
        self.assertAlmostEqual(p["diff"], 2 / 3 - 0.5)

    def test_paired_comparison_drops_items_unjudged_on_either_side(self):
        """README 주 측정값: 대조군 하나하나와 양쪽 모두 판정된 같은 항목끼리 비교, 차이 = 비교들의 평균."""
        b = self.tmp
        mk_run(b, "ctl1", "control", {"K1": "pass", "K2": "pass", "K3": "fail"}, 0)
        mk_run(b, "ctl2", "control", {"K1": "pass", "K2": "unscorable", "K3": "fail"}, 0)
        mk_run(b, "cmp1", "compact", {"K1": "fail", "K2": "fail", "K3": "pass"}, 1)
        res, _ = summarize(b)
        p = [x for x in res["pairs"] if x["run_id"] == "cmp1"][0]
        # ctl1: K1~K3 → 2/3 − 1/3 ; ctl2: K1·K3만 → 1/2 − 1/2
        self.assertAlmostEqual(p["diff"], ((2 / 3 - 1 / 3) + 0.0) / 2)
        self.assertEqual(len(p["comparisons"]), 2, p)

    def test_stale_score_is_reported_as_mismatch_not_used(self):
        b = self.tmp
        mk_run(b, "ctl", "control", {"K1": "pass"}, 0)
        d = mk_run(b, "cmp", "compact", {"K1": "fail"}, 1)
        rj = json.loads((d / "run.json").read_text())
        rj["warnings"] = ["changed after scoring"]
        (d / "run.json").write_text(json.dumps(rj))
        res, md = summarize(b)
        self.assertIn("cmp", {e["run_id"] for e in res["excluded"]})
        self.assertEqual(self.row(res, "claude-sonnet-5-5", "compact")["included"], 0)
        self.assertIn("cmp", md)

    def test_alias_and_drop_candidates_not_in_main_rate(self):
        b = self.tmp
        mk_run(b, "ctl1", "control", {"K1": "pass", "D0-13": "fail", "D6-04": ("fail", {"alias_of": "D0-13"}),
                                      "D2-04": ("fail", {"drop_candidate": True})}, 0)
        res, _ = summarize(b)
        r = self.row(res, "claude-sonnet-5-5", "control")
        self.assertEqual(r["judged"], 2)            # K1·D0-13 (별칭·버릴 후보 제외)
        self.assertAlmostEqual(r["violation_rate"], 0.5)
        self.assertEqual(r["drop_candidate_judged"], 1)

    def test_unjudged_results_outside_denominator(self):
        b = self.tmp
        mk_run(b, "ctl1", "control", {"K1": "pass", "K2": "fail", "D0-02": "not_run", "D0-03": "unscorable",
                                      "D3-01": "premise_unmet"}, 0)
        res, _ = summarize(b)
        r = self.row(res, "claude-sonnet-5-5", "control")
        self.assertEqual(r["judged"], 2)
        self.assertAlmostEqual(r["violation_rate"], 0.5)

    def test_invalid_unscored_and_incomplete_runs_are_reported_not_dropped(self):
        b = self.tmp
        mk_run(b, "ok", "compact", {"K1": "fail"}, 1)
        mk_run(b, "ctl", "control", {"K1": "pass"}, 0)
        mk_run(b, "inv", "compact", {"K1": "fail"}, 0, invalid=True)
        mk_run(b, "nosc", "compact", {}, 0, score=False)
        mk_run(b, "abort", "compact", {"K1": "fail"}, 1, mc=False)
        res, md = summarize(b)
        excluded = {e["run_id"]: e for e in res["excluded"]}
        self.assertEqual(set(excluded), {"inv", "nosc", "abort"})
        self.assertIn("no_compaction", excluded["inv"]["invalid_reasons"])
        r = self.row(res, "claude-sonnet-5-5", "compact")
        self.assertEqual(r["runs"], 4)
        self.assertEqual(r["included"], 1)
        self.assertEqual(r["invalid"], 1)
        self.assertEqual(r["unscored"], 1)
        self.assertEqual(r["judged"], 1)   # 집계 대상(ok)의 항목만
        for rid in ("inv", "nosc", "abort"):
            self.assertIn(rid, md)

    def test_input_mismatch_is_flagged_and_not_mixed(self):
        for key, val in (("messages_sha256_all", "other-msgs"), ("skeleton_commit", "other-skel"),
                         ("cli_version", "2.1.999"), ("image_id", "sha256:other")):
            with self.subTest(key=key):
                b = self.tmp / key
                mk_run(b, "c1", "control", {"K1": "pass"}, 0)
                mk_run(b, "c2", "control", {"K1": "pass"}, 0)
                mk_run(b, "m1", "compact", {"K1": "pass"}, 1)
                mk_run(b, "m2", "compact", {"K1": "pass"}, 1)
                mk_run(b, "odd", "compact", {"K1": "fail"}, 1, **{key: val})
                res, _ = summarize(b)
                ex = {e["run_id"]: e for e in res["excluded"]}
                self.assertIn("odd", ex)
                self.assertTrue(ex["odd"]["input_mismatch"])
                r = self.row(res, "claude-sonnet-5-5", "compact")
                self.assertEqual(r["input_mismatch"], 1)
                self.assertEqual(r["violation_rate"], 0.0)   # odd의 fail이 섞이지 않음

    def test_condition_inputs_compared_within_condition(self):
        b = self.tmp
        mk_run(b, "c1", "control", {"K1": "pass"}, 0)
        mk_run(b, "o1", "outbox", {"K1": "pass"}, 1, settings_sha256="outbox-settings", outbox_config_sha256="ob")
        mk_run(b, "o2", "outbox", {"K1": "pass"}, 1, settings_sha256="outbox-settings", outbox_config_sha256="ob")
        mk_run(b, "o3", "outbox", {"K1": "fail"}, 1, settings_sha256="outbox-settings", outbox_config_sha256="ob-changed")
        res, _ = summarize(b)
        ex = {e["run_id"]: e for e in res["excluded"]}
        self.assertEqual(set(ex), {"o3"})   # 조건마다 settings가 다른 것은 불일치가 아님


if __name__ == "__main__":
    unittest.main()
