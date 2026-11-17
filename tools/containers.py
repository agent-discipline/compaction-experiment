"""Docker 실행 계층 — 실행기(run_task)·채점기(score_run·db_catalog)의 docker CLI 호출을 한곳에 모은다.

- 모든 호출은 `Docker.run(...)` 하나를 지난다. 테스트는 `cfg["docker_bin"]`을 가짜 docker(tests/fake_docker.py)로 바꾼다.
- 이 실험이 만드는 리소스(컨테이너·네트워크·볼륨)는 전부 이름이 `cfg["resource_prefix"]`(기본 `cx-`)로 시작하고
  레이블 `cx.experiment=compaction-experiment`를 단다. 지우기는 이 접두를 가진 **정확한 이름**으로만 한다
  (prune·필터 삭제 없음). `payment-`로 시작하는 이름은 접두와 무관하게 거부한다(같은 머신의 다른 실험).
"""
import json
import subprocess
import time

EXPERIMENT_LABEL = "cx.experiment=compaction-experiment"
PROTECTED_PREFIXES = ("payment-",)
KINDS = ("container", "network", "volume")


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, bin="docker", prefix="cx-"):
        if not prefix or any(prefix.startswith(p) for p in PROTECTED_PREFIXES):
            raise ValueError(f"리소스 접두가 비었거나 보호된 접두와 겹친다: {prefix!r}")
        self.bin, self.prefix = bin, prefix

    @classmethod
    def from_cfg(cls, cfg):
        return cls(cfg.get("docker_bin", "docker"), cfg.get("resource_prefix", "cx-"))

    def run(self, *args, input=None, timeout=120):
        """docker CLI 한 번. CompletedProcess(text). 시간 초과는 subprocess.TimeoutExpired 그대로."""
        return subprocess.run([self.bin, *map(str, args)], input=input, capture_output=True, text=True, timeout=timeout)

    def guard(self, name):
        if not name.startswith(self.prefix) or name.startswith(PROTECTED_PREFIXES):
            raise ValueError(f"이 실험이 만들지 않은 이름은 다루지 않는다: {name!r}")
        return name

    def exists(self, kind, name):
        return self.run(kind, "inspect", name).returncode == 0

    def check(self, p, what):
        if p.returncode != 0:
            raise DockerError(f"{what} 실패(rc={p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
        return p

    def create_network(self, name, run_label, internal=False):
        args = ["network", "create", "--label", EXPERIMENT_LABEL, "--label", f"cx.run={run_label}"]
        args += ["--internal"] if internal else []
        self.check(self.run(*args, self.guard(name)), f"network create {name}")

    def create_volume(self, name, run_label):
        self.check(self.run("volume", "create", "--label", EXPERIMENT_LABEL, "--label", f"cx.run={run_label}",
                            self.guard(name)), f"volume create {name}")

    def remove(self, kind, name, timeout=120):
        """정확한 이름 하나를 지운다. 없으면 성공으로 본다. 반환: None(성공) 또는 오류 문자열."""
        self.guard(name)
        if not self.exists(kind, name):
            return None
        args = {"container": ["rm", "-f", name], "network": ["network", "rm", name], "volume": ["volume", "rm", name]}[kind]
        try:
            p = self.run(*args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"{kind} {name}: 시간 초과"
        if p.returncode != 0 and self.exists(kind, name):
            return f"{kind} {name}: rc={p.returncode} {p.stderr.strip()[:300]}"
        return None

    def image_info(self, tag):
        """{id, labels, size}. 없으면 DockerError."""
        p = self.check(self.run("image", "inspect", "--format", "{{json .}}", tag), f"image inspect {tag}")
        d = json.loads(p.stdout)
        return {"id": d.get("Id"), "labels": (d.get("Config") or {}).get("Labels") or {}, "size": d.get("Size")}

    def container_state(self, name):
        """{exit_code, oom_killed, status, error} — Env 등 설정 값은 읽지 않는다. 실패하면 {inspect_error}."""
        p = self.run("container", "inspect", "--format", "{{json .State}}", name)
        if p.returncode != 0:
            return {"inspect_error": p.stderr.strip()[:300]}
        s = json.loads(p.stdout)
        return {"exit_code": s.get("ExitCode"), "oom_killed": s.get("OOMKilled"), "status": s.get("Status"),
                "error": s.get("Error") or None}

    def names(self, all=False, label=None):
        args = ["ps", "--format", "{{.Names}}"] + (["-a"] if all else []) + (["--filter", f"label={label}"] if label else [])
        p = self.check(self.run(*args), "ps")
        return [x for x in p.stdout.split() if x]


def postgres_args(db, name, network, run_label, data_mount):
    """PostgreSQL 사이드카 `docker run -d` 인자. db = experiment.json의 "db"."""
    return ["run", "-d", "--name", name, "--label", EXPERIMENT_LABEL, "--label", f"cx.run={run_label}",
            "--network", network, "--network-alias", db["alias"], "--mount", data_mount,
            "-e", f"POSTGRES_USER={db['user']}", "-e", f"POSTGRES_PASSWORD={db['password']}",
            "-e", f"POSTGRES_DB={db['name']}", db["image"], *db.get("server_args", [])]


def start_postgres(docker, db, name, network, run_label, data_mount, ready_timeout=120):
    docker.guard(name)
    docker.check(docker.run(*postgres_args(db, name, network, run_label, data_mount), timeout=600),
                 f"postgres 사이드카 {name}")
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        # -h 127.0.0.1: 이미지 초기화 중의 임시 서버(유닉스 소켓만)가 아니라 실제 서버를 기다린다
        if docker.run("exec", name, "pg_isready", "-U", db["user"], "-d", db["name"], "-h", "127.0.0.1").returncode == 0:
            return
        time.sleep(0.5)
    raise DockerError(f"postgres 사이드카 {name}가 {ready_timeout}초 안에 준비되지 않음")


def jdbc_env(db):
    """에이전트·채점 컨테이너에 넣는 DB 환경 변수(골격 application.yml이 쓰는 이름)."""
    return {"DB_URL": f"jdbc:postgresql://{db['alias']}:5432/{db['name']}", "DB_USERNAME": db["user"],
            "DB_PASSWORD": db["password"]}


class PostgresStack:
    """채점용(그리고 --check-env용) 일회용 네트워크 + PostgreSQL 사이드카(데이터는 tmpfs). with 블록이 끝나면 지운다.
    정리 오류는 `cleanup_errors`에 남는다(예외로 덮지 않음 — 호출자가 기록한다)."""

    def __init__(self, docker, db, tag, internal):
        self.docker, self.db, self.internal = docker, db, internal
        self.network, self.name = docker.guard(f"{tag}-net"), docker.guard(f"{tag}-db")
        self.tag, self.cleanup_errors = tag, []

    def __enter__(self):
        try:
            self.docker.create_network(self.network, self.tag, internal=self.internal)
            start_postgres(self.docker, self.db, self.name, self.network, self.tag,
                           "type=tmpfs,dst=/var/lib/postgresql/data")
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        for kind, n in (("container", self.name), ("network", self.network)):
            err = self.docker.remove(kind, n)
            if err:
                self.cleanup_errors.append(err)

    def __exit__(self, *exc):
        self.close()
        return False

    def psql(self, dbname, *args, input=None, timeout=120):
        return self.docker.run("exec", "-i", self.name, "psql", "-U", self.db["user"], "-h", "127.0.0.1", "-X", "-q",
                               "-v", "ON_ERROR_STOP=1", "-d", dbname, *args, input=input, timeout=timeout)
