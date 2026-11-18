"""숨긴 계약 테스트(H) — 최종 작업 폴더 사본에 숨긴 테스트를 덮어 실행하고 JUnit XML을 항목에 대응한다.

items.json(hidden/README.md): {"items": [{"id", "class", "display_name", "method", "alias_of"?, "no_test"?}, ...]}.
대응: JUnit XML <testcase classname=class name=display_name>. 테스트 지정 문자열은 내부적으로
"<class>#<display_name>"('#' 없으면 클래스 전체 — 중첩 `$…` 포함). 간단한 형식 {"<id>": ["<지정>", ...]}도 받는다.
판정: failure/error = fail(메시지가 unscorable_pattern에 맞으면 unscorable) · skipped = unscorable.
"""
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def load_items(path):
    """(id → 테스트 지정 목록, id → {alias_of, no_test}). no_test 항목은 지정 목록이 빈 리스트."""
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    mapping, meta = {}, {}
    if isinstance(data, list):
        for x in data:
            if x.get("no_test"):
                mapping[x["id"]] = []
            elif "tests" in x:
                mapping[x["id"]] = [str(t) for t in x["tests"]]
            elif x.get("class") and x.get("display_name"):
                mapping[x["id"]] = [f"{x['class']}#{x['display_name']}"]
            else:
                raise ValueError(f"items.json 항목에 class·display_name이 없음: {x.get('id')}")
            meta[x["id"]] = {"alias_of": x.get("alias_of"), "no_test": x.get("no_test")}
    elif isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
        mapping = {k: [str(t) for t in v] for k, v in data.items() if not k.startswith("_")}
        meta = {k: {"alias_of": None, "no_test": None} for k in mapping}
    else:
        raise ValueError(f"items.json 형식을 해석하지 못함: {path}")
    return mapping, meta


def parse_junit(junit_dir, unscorable_pattern=None):
    """[{classname, name, status(pass|fail|unscorable|skipped), message}]"""
    pat = re.compile(unscorable_pattern) if unscorable_pattern else None
    out = []
    for x in sorted(Path(junit_dir).glob("*.xml")):
        root = ET.parse(x).getroot()
        for tc in root.iter("testcase"):
            status, msg = "pass", ""
            for tag in ("failure", "error"):
                el = tc.find(tag)
                if el is not None:
                    msg = f"{el.get('type', '')}: {el.get('message', '')}\n{(el.text or '')[:2000]}"
                    status = "unscorable" if pat and pat.search(el.get("message") or "") else "fail"
                    break
            else:
                sk = tc.find("skipped")
                if sk is not None:
                    status, msg = "unscorable", f"skipped: {sk.get('message', '')}"
            out.append({"classname": tc.get("classname", ""), "name": tc.get("name", ""),
                        "status": status, "message": msg[:2000]})
    return out


def _method(name):
    return re.sub(r"\(.*\)$", "", name).strip()


def match(spec, case):
    if "#" in spec:
        cls, meth = spec.split("#", 1)
        return case["classname"] == cls and (case["name"] == meth or _method(case["name"]) == _method(meth))
    return case["classname"] == spec or case["classname"].startswith(spec + "$")


def item_results(items, cases):
    """{item: {result, tests:[…]}} — 실패 하나라도 fail, 아니면 채점 불가 하나라도 unscorable, 없으면 not_run."""
    out = {}
    for item, specs in items.items():
        if not specs:
            out[item] = {"result": "unscorable", "reason": "no_test", "tests": []}
            continue
        hit = [c for c in cases if any(match(s, c) for s in specs)]
        if not hit:
            res = "not_run"
        elif any(c["status"] == "fail" for c in hit):
            res = "fail"
        elif any(c["status"] == "unscorable" for c in hit):
            res = "unscorable"
        else:
            res = "pass"
        out[item] = {"result": res, "reason": "no_matching_testcase" if not hit else None,
                     "tests": [{"test": f"{c['classname']}#{c['name']}", "status": c["status"],
                                "message": c["message"][:500]} for c in hit]}
    return out


# 빌드 실패의 원인 구분(Gradle 출력) — 채점 환경 쪽이면 에이전트 코드의 판정에 쓰지 않는다(scoring-items.md "숨긴 테스트 빌드 실패").
ENV_FAILURE = re.compile(
    r"Could not resolve|Could not download|Could not GET|Could not HEAD|Could not determine the dependencies"
    r"|Could not install Gradle distribution|UnknownHostException|Connection refused|Connection reset|Connect timed out"
    r"|connect timed out|Read timed out|No route to host|Network is unreachable|PKIX path building failed"
    r"|No space left on device|Timeout waiting to lock|Gradle could not start your build|Could not create service of type"
    r"|Unable to find image|Cannot connect to the Docker daemon|OCI runtime")
COMPILE_FAILURE = re.compile(r"Execution failed for task '[^']*:(compileKotlin|compileJava|processResources)'"
                             r"|Compilation error|Compilation failed|^e: ", re.M)
DOCKER_RUN_FAILED = (125, 126, 127)  # docker run 자체가 컨테이너·명령을 시작하지 못함


def failure_kind(rc, log_text):
    """빌드 실패의 원인: environment(채점 환경 — 네트워크·의존성 받기·docker 실행 실패) · code(컴파일 오류 표지) ·
    unknown(둘 다 아님). 환경 표지가 있으면 컴파일 표지보다 앞선다(의존성을 못 받으면 컴파일 판정이 성립하지 않는다)."""
    if rc in DOCKER_RUN_FAILED or ENV_FAILURE.search(log_text):
        return "environment"
    if COMPILE_FAILURE.search(log_text):
        return "code"
    return "unknown"


def _execute(execute, cmd, work_copy, log, timeout):
    try:
        return execute(cmd, work_copy, log, timeout), False
    except subprocess.TimeoutExpired:
        return None, True


def run_hidden(work_copy, hidden_cfg, hidden_dir, log_path, execute):
    """work_copy(이미 복사된 최종 작업 폴더)에서 먼저 메인 코드만 컴파일하고(`main_build_command`), 되면 숨긴 테스트를
    덮고 명령을 돌린다. 메인 컴파일이 실패하거나 시간을 넘기면 숨긴 테스트는 돌리지 않는다(cases 빈 목록).
    execute(cmd, work_copy, log_file, timeout) → 종료 코드(시간 초과면 subprocess.TimeoutExpired) — 채점기는 채점 컨테이너
    (score_run.container_executor)를 준다. {main_build{exit_code, timed_out, failure_kind?}, exit_code, timed_out, cases,
    failure_kind?(testcase 0개일 때 숨긴 테스트 단계 로그의 원인)}"""
    work_copy = Path(work_copy)
    for d in ("build", ".gradle", ".kotlin", *hidden_cfg.get("pre_delete", [])):
        shutil.rmtree(work_copy / d, ignore_errors=True)
    gw = work_copy / "gradlew"
    if gw.is_file():
        os.chmod(gw, 0o755)
    timeout = hidden_cfg.get("timeout_s", 3600)
    with open(log_path, "wb") as log:
        log.write(b"=== main build: " + " ".join(hidden_cfg["main_build_command"]).encode() + b"\n")
        log.flush()
        rc, timed_out = _execute(execute, hidden_cfg["main_build_command"], work_copy, log, timeout)
    main_build = {"exit_code": rc, "timed_out": timed_out}
    if timed_out or rc != 0:
        if not timed_out:
            main_build["failure_kind"] = failure_kind(rc, Path(log_path).read_text(encoding="utf-8", errors="replace"))
        return {"main_build": main_build, "exit_code": None, "timed_out": False, "cases": []}
    start = Path(log_path).stat().st_size
    for src, dst in hidden_cfg["overlay"]:
        s = Path(hidden_dir) / src
        if not s.exists():
            raise FileNotFoundError(f"숨긴 테스트 원본 없음: {s}")
        if s.is_dir():
            shutil.copytree(s, work_copy / dst, dirs_exist_ok=True)
        else:
            (work_copy / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, work_copy / dst)
    with open(log_path, "ab") as log:
        log.write(b"\n=== hidden tests: " + " ".join(hidden_cfg["command"]).encode() + b"\n")
        log.flush()
        rc, timed_out = _execute(execute, hidden_cfg["command"], work_copy, log, timeout)
    jd = work_copy / hidden_cfg["junit_dir"]
    cases = parse_junit(jd, hidden_cfg.get("unscorable_pattern")) if jd.is_dir() else []
    out = {"main_build": main_build, "exit_code": rc, "timed_out": timed_out, "cases": cases}
    if not cases and not timed_out:
        with open(log_path, "rb") as f:
            f.seek(start)
            out["failure_kind"] = failure_kind(rc, f.read().decode("utf-8", errors="replace"))
    return out
