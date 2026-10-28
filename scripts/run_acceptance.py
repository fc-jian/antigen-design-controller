"""Finite staged acceptance for an existing run; never substitutes or fabricates results."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
from antigen_design.io import read_json, write_json
from verify_acceptance import verify


def wait_for_controller(root):
    with open(root / ".controller.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return
            except BlockingIOError:
                time.sleep(15)


def boundary(root, evidence):
    plan = read_json(root / "plan.json")
    manifest = read_json(root / "manifest.json")
    for name, task in plan["tasks"].items():
        state = manifest["tasks"][name]
        if task["stage"] in ("rfd3", "mpnn", "msa"):
            if state["state"] != "succeeded":
                raise ValueError(f"MSA boundary incomplete: {name}")
        elif state["state"] != "pending" or state.get("job_id"):
            raise ValueError("Strict staged acceptance requires GPU folding not to have been submitted yet")
    write_json(evidence / "msa-boundary.json", manifest)


def run_command(argv, root, evidence, retries=3):
    """Retry known connection failures only; never auto-retry failed or ambiguous jobs."""
    for attempt in range(retries + 1):
        tail = ""
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                tail = (tail + line)[-65536:]
            code = process.wait()
        if code == 0:
            return
        manifest = read_json(root / "manifest.json")
        safe = all(s["state"] not in ("failed", "blocked", "submitting") for s in manifest["tasks"].values())
        transient = any(s in tail.lower() for s in ("timeoutexpired", "connection timed out", "connection reset", "connection closed", "kex_exchange_identification"))
        if attempt == retries or not safe or not transient:
            raise subprocess.CalledProcessError(code, argv, output=tail)
        path = evidence / "transport-retries.json"
        events = read_json(path) if path.exists() else []
        events.append({"boundary_argv": argv[3:], "retry": attempt + 1, "reason": "transient_connection_failure",
                       "existing_jobs": {name: s.get("job_id") for name, s in manifest["tasks"].items() if s.get("job_id")}})
        write_json(path, events)
        print("Retrying connection to existing jobs after transport failure", flush=True)
        time.sleep(15)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("directory")
    p.add_argument("--evidence-dir", required=True)
    p.add_argument("--wait-for-controller", action="store_true")
    a = p.parse_args()
    root, evidence = Path(a.directory).resolve(), Path(a.evidence_dir).resolve()
    if not (root / "plan.json").is_file():
        raise ValueError("Initialize a real run before running acceptance")
    evidence.mkdir(parents=True, exist_ok=True)
    write_json(evidence / "status.json", {"state": "running", "run": str(root)})
    def command(*args):
        print("Acceptance:", *args, flush=True)
        run_command([sys.executable, "-m", "antigen_design.cli", *map(str, args)], root, evidence)
    try:
        if a.wait_for_controller:
            wait_for_controller(root)
        command("resume", root, "--until", "msa", "--wait")
        boundary(root, evidence)
        command("resume", root, "--only", "fold", "--wait")
        write_json(evidence / "fold-boundary.json", read_json(root / "manifest.json"))
        command("resume", root, "--only", "metrics", "--wait")
        command("report", root)
        command("collect", root)
        result = verify(root)
        write_json(evidence / "verification.json", result)
        write_json(evidence / "status.json", {"state": "succeeded", "run": str(root)})
        print(json.dumps(result, indent=2), flush=True)
    except Exception as exc:
        write_json(evidence / "status.json", {"state": "failed", "run": str(root), "error": str(exc)})
        raise


if __name__ == "__main__":
    main()
