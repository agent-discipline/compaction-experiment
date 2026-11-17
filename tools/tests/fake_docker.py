#!/usr/bin/env python3
"""테스트용 가짜 `docker` — 실행기가 쓰는 docker CLI 부분집합을 컨테이너 없이 흉내 낸다(cfg["docker_bin"]으로 바꿔 끼움).

상태는 FAKE_DOCKER_STATE 폴더: containers/<이름>.json · networks/<이름> · volumes/<이름>/(볼륨 내용) · calls.jsonl(호출 기록).
컨테이너 실행 = 마운트 대상 경로를 호스트 폴더로 바꿔(bind → src, volume → volumes/<이름>, tmpfs → 임시 폴더) 명령을
호스트에서 돌린다. `claude` → tests/fake_claude.py. 훅 명령의 컨테이너 경로는 FAKE_PATH_MAP(JSON)으로 fake_claude가 바꾼다.
조작: FAKE_DOCKER_PS(쉼표 목록 — 실행 중인 다른 컨테이너 이름) · FAKE_DOCKER_FAIL_RM(이 문자열이 든 이름의 rm 실패).
`<kind> inspect --format …` = 레이블 조회(컨테이너는 `Labels`가 든 형식일 때만, 그 밖은 State).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
VALUE_OPTS = {"--name", "--label", "--network", "--network-alias", "--workdir", "-w", "--env", "-e", "--env-file",
              "--mount", "--user", "--format", "--filter", "--time", "-t"}
FLAG_OPTS = {"--interactive", "-i", "--init", "-d", "--rm", "--attach", "-a", "-f", "--internal", "-At", "-q"}


def log_call(argv):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "calls.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")


def parse(args):
    """옵션(값 목록) · 위치 인자."""
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if pos:  # 이미지(첫 위치 인자) 뒤는 전부 명령
            pos.append(a)
        elif a in VALUE_OPTS:
            opts.setdefault(a, []).append(args[i + 1])
            i += 1
        elif a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts.setdefault(k, []).append(v)
        elif a in FLAG_OPTS:
            opts.setdefault(a, []).append(True)
        else:
            pos.append(a)
        i += 1
    return opts, pos


def cpath(name):
    return STATE / "containers" / f"{name}.json"


def load(name):
    p = cpath(name)
    return json.loads(p.read_text()) if p.is_file() else None


def save(c):
    cpath(c["name"]).parent.mkdir(parents=True, exist_ok=True)
    cpath(c["name"]).write_text(json.dumps(c))


def label_file(kind, name):
    return STATE / "labels" / f"{kind}-{name}.json"


def labels_of(items):
    return dict(x.split("=", 1) if "=" in x else (x, "") for x in items)


def fail(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)


def make_container(opts, pos):
    name = opts["--name"][0]
    if load(name):
        fail(f"Conflict. The container name \"/{name}\" is already in use")
    mounts = []
    for k, spec in enumerate(opts.get("--mount", [])):
        kv = dict(x.split("=", 1) if "=" in x else (x, True) for x in spec.split(","))
        if kv["type"] == "bind":
            src = kv["src"]
            if not Path(src).exists():
                fail(f"bind source path does not exist: {src}")
        elif kv["type"] == "volume":
            src = str(STATE / "volumes" / kv["src"])
            Path(src).mkdir(parents=True, exist_ok=True)  # docker도 없는 볼륨은 만든다
        else:
            src = str(STATE / "tmpfs" / name / str(k))
            Path(src).mkdir(parents=True, exist_ok=True)
        mounts.append([kv["dst"], src])
    env = {}
    for f in opts.get("--env-file", []):
        for line in Path(f).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k] = v
    for e in opts.get("--env", []) + opts.get("-e", []):
        k, _, v = e.partition("=")
        env[k] = v
    c = {"name": name, "image": pos[0], "cmd": pos[1:], "mounts": sorted(mounts, key=lambda m: -len(m[0])),
         "env": env, "workdir": (opts.get("--workdir") or opts.get("-w") or ["/"])[0],
         "labels": opts.get("--label", []), "network": (opts.get("--network") or [None])[0],
         "user": (opts.get("--user") or [None])[0], "state": {"Status": "created", "ExitCode": 0, "OOMKilled": False}}
    save(c)
    return c


def host_path(c, p):
    for dst, src in c["mounts"]:
        if p == dst or p.startswith(dst + "/"):
            return src + p[len(dst):]
    return p


def execute(c, stdin=None):
    if c["image"].startswith("postgres"):
        c["state"]["Status"] = "running"
        save(c)
        return 0
    cmd = list(c["cmd"])
    if not cmd or cmd == ["true"]:
        return 0
    if cmd[0] == "claude":
        cmd = [sys.executable, str(FAKE_CLAUDE), *cmd[1:]]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_") and k != "FAKE_DOCKER_STATE"})
    env.update({k: host_path(c, v) for k, v in c["env"].items()})
    env["FAKE_PATH_MAP"] = json.dumps(c["mounts"])
    cwd = host_path(c, c["workdir"])
    p = subprocess.run(cmd, cwd=cwd, env=env, input=stdin)
    c["state"].update(Status="exited", ExitCode=p.returncode)
    save(c)
    return p.returncode


def docker_cp(src, dest):
    name, path = src.split(":", 1)
    c = load(name) or fail(f"No such container: {name}")
    hp = Path(host_path(c, path.rstrip(".").rstrip("/") if path.endswith("/.") else path))
    if not hp.exists():
        fail(f"Could not find the file {path} in container {name}")
    dest = Path(dest)
    if path.endswith("/."):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(hp, dest, dirs_exist_ok=True)
    elif hp.is_dir():
        shutil.copytree(hp, dest / hp.name if dest.is_dir() else dest, dirs_exist_ok=True)
    else:
        shutil.copy2(hp, dest / hp.name if dest.is_dir() else dest)


def main(argv):
    log_call(argv)
    cmd, rest = argv[0], argv[1:]
    if cmd in ("network", "volume"):
        sub, rest = rest[0], rest[1:]
        opts, pos = parse(rest)
        d = STATE / (cmd + "s")
        if sub == "create":
            if (d / pos[0]).exists():
                fail(f"{cmd} with name {pos[0]} already exists")
            (d / pos[0]).mkdir(parents=True)
            label_file(cmd, pos[0]).parent.mkdir(parents=True, exist_ok=True)
            label_file(cmd, pos[0]).write_text(json.dumps(labels_of(opts.get("--label", []))))
            print(pos[0])
        elif sub == "inspect":
            if not (d / pos[0]).exists():
                sys.exit(1)
            if "--format" in opts:  # 레이블 조회만 흉내
                lf = label_file(cmd, pos[0])
                print(lf.read_text() if lf.is_file() else "null")
            sys.exit(0)
        elif sub == "rm":
            if os.environ.get("FAKE_DOCKER_FAIL_RM", "\0") in pos[0]:
                fail("fake rm failure")
            shutil.rmtree(d / pos[0], ignore_errors=True)
        return 0
    if cmd == "image":
        opts, pos = parse(rest[1:])
        print(json.dumps({"Id": "sha256:fakeimage", "Size": 1, "Config": {"Labels": {
            "cx.uid": str(os.getuid()), "cx.gid": str(os.getgid()), "cx.claude-code": "fake"}}}))
        return 0
    if cmd == "container" and rest[0] == "inspect":
        opts, pos = parse(rest[1:])
        c = load(pos[0]) or fail(f"No such container: {pos[0]}")
        fmt = (opts.get("--format") or [""])[0]
        print(json.dumps(labels_of(c["labels"]) if "Labels" in fmt else c["state"]))
        return 0
    if cmd == "ps":
        opts, pos = parse(rest)
        out = []
        for p in sorted((STATE / "containers").glob("*.json")) if (STATE / "containers").is_dir() else []:
            c = json.loads(p.read_text())
            if "-a" not in opts and c["state"]["Status"] != "running":
                continue
            want = [f.split("=", 1)[1] for f in opts.get("--filter", []) if f.startswith("label=")]
            if all(w in c["labels"] for w in want):
                out.append(c["name"])
        if not opts.get("--filter"):
            out += [x for x in os.environ.get("FAKE_DOCKER_PS", "").split(",") if x]
        print("\n".join(out))
        return 0
    if cmd == "create":
        opts, pos = parse(rest)
        make_container(opts, pos)
        return 0
    if cmd == "run":
        opts, pos = parse(rest)
        c = make_container(opts, pos)
        rc = execute(c, None)
        if "--rm" in opts:
            cpath(c["name"]).unlink()
        return rc
    if cmd == "start":
        opts, pos = parse(rest)
        c = load(pos[0]) or fail(f"No such container: {pos[0]}")
        return execute(c, sys.stdin.buffer.read() if "--interactive" in opts else None)
    if cmd == "exec":
        opts, pos = parse(rest)
        c = load(pos[0]) or fail(f"No such container: {pos[0]}")
        return 0 if pos[1] == "pg_isready" else fail("fake exec: unsupported")
    if cmd == "cp":
        docker_cp(rest[0], rest[1])
        return 0
    if cmd == "rm":
        opts, pos = parse(rest)
        if os.environ.get("FAKE_DOCKER_FAIL_RM", "\0") in pos[0]:
            fail("fake rm failure")
        if cpath(pos[0]).is_file():
            cpath(pos[0]).unlink()
        return 0
    if cmd in ("kill", "stop"):
        return 0
    fail(f"fake docker: unsupported {argv}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
