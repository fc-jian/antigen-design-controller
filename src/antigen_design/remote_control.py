"""Small server-side control operations invoked through ssh-skill from WSL."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tarfile
from .io import read_json, write_json, digest
from .worker import receipt_valid
from .scheduler import slurm_script
from .stages import resource_key


def inspect(plan, requests):
    from . import baremetal
    if baremetal.enabled(plan):
        return baremetal.inspect(plan, requests)
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.inspect(plan, requests)
    root = Path(plan["config"]["output"])
    ids = [r["job_id"] for r in requests.values() if r.get("job_id")]
    states = {}
    if ids:
        out = subprocess.run(["sacct", "-n", "-P", "-X", "-j", ",".join(ids), "--format=JobIDRaw,State,ExitCode"],
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            fields = line.split("|")
            if len(fields) >= 3 and fields[0] in ids:
                state = fields[1].split()[0].rstrip("+")
                if state == "COMPLETED" and fields[2] != "0:0":
                    state = "FAILED"
                states[fields[0]] = state
    result = {}
    for name, request in requests.items():
        task = plan["tasks"][name]
        directory = root / "tasks" / name
        info = {"slurm_state": states.get(request.get("job_id"), "UNKNOWN")}
        if request.get("state") == "succeeded" or info["slurm_state"] == "COMPLETED":
            info["valid"] = receipt_valid(directory, task["signature"])
            if info["valid"] and task["stage"] == "metrics":
                info["metrics"] = read_json(directory / "metrics.json")
        error = directory / "error.json"
        if error.exists():
            info["error"] = read_json(error)
        log = root / "logs" / f"{name}_a{request['attempts']}.log"
        if log.is_file() and info["slurm_state"] not in ("RUNNING", "PENDING"):
            with open(log, "rb") as handle:
                handle.seek(max(0, log.stat().st_size - 16000))
                info["log_tail"] = handle.read().decode(errors="replace")
        submission = root / "control" / f"{name}_a{request['attempts']}.json"
        if request.get("state") == "submitting" and submission.is_file():
            info["recovered_job_id"] = read_json(submission).get("job_id")
        result[name] = info
    return result


def submit(plan, request):
    from . import baremetal
    if baremetal.enabled(plan):
        return baremetal.submit(plan, request)
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.submit(plan, request)
    c, name, attempt = plan["config"], request["task"], request["attempt"]
    root = Path(c["output"])
    control = root / "control"
    control.mkdir(exist_ok=True)
    record = control / f"{name}_a{attempt}.json"
    with open(control / f"{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if record.exists():
            result = read_json(record)
            if result.get("job_id") or result.get("rejected"):
                return result
            raise RuntimeError("Previous submission result is unknown; inspect Slurm before retrying")
        task = plan["tasks"][name]
        tier = request.get("memory_tier", 0)
        if tier != 0:
            raise ValueError("P2 disables automatic memory tiers")
        resources = dict(c["execution"]["resources"][resource_key(c, task["stage"])])
        if task["stage"] == "metrics":
            resources = {k: v for k, v in c["execution"]["resources"]["cpu"].items() if k in ("account", "qos", "partition")} | resources
        directory = root / "tasks" / name
        if directory.exists():
            old = root / "attempts" / f"{name}_before_a{attempt}"
            old.parent.mkdir(exist_ok=True)
            directory.rename(old)
        log = root / "logs" / f"{name}_a{attempt}.log"
        argv = [c["execution"]["python"][task["stage"]], "-m", "antigen_design.worker",
                str(root / "plan.json"), name, "--memory-tier", str(tier), "--attempt", str(attempt)]
        if c["execution"].get("source_dir"):
            argv = ["env", "PYTHONPATH=" + c["execution"]["source_dir"], *argv]
        script = root / "scripts" / f"{name}_a{attempt}.sh"
        script.write_text(slurm_script(argv, root, log, resources, f"ah_{plan['identity'][:8]}_{name}_a{attempt}"))
        write_json(record, {"state": "submitting", "memory_tier": tier, "resources": resources})
        completed = subprocess.run(["sbatch", "--parsable", "--chdir", str(root), str(script)], capture_output=True, text=True)
        if completed.returncode:
            result = {"rejected": completed.stderr.strip() or completed.stdout.strip(), "resources": resources}
            write_json(record, result)
            return result
        raw = completed.stdout.strip()
        job = raw.split(";")[0]
        if not job.isdigit():
            raise RuntimeError(f"Unexpected sbatch response {raw}")
        result = {"job_id": job, "memory_tier": tier, "resources": resources}
        write_json(record, result)
        return result


def reconcile(plan, request):
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.reconcile(plan, request)
    """Attach a known scheduler job only after exact owner/name/workdir matching."""
    import getpass
    name, job = request["task"], str(request["job_id"])
    attempt = request["attempt"]
    if name not in plan["tasks"] or not job.isdigit():
        raise ValueError("Unknown task or invalid job ID")
    root = Path(plan["config"]["output"])
    record = root / "control" / f"{name}_a{attempt}.json"
    with open(root / "control" / f"{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_json(record)
        if previous.get("rejected") or previous.get("job_id") not in (None, job):
            raise ValueError("Submission record cannot be reconciled to this job")
        out = subprocess.check_output(["sacct", "-n", "-P", "-X", "-j", job,
            "--format=JobIDRaw,JobName%200,WorkDir%500,User"], text=True)
        expected = [job, f"ah_{plan['identity'][:8]}_{name}_a{attempt}", str(root), getpass.getuser()]
        matches = [line.strip().split("|")[:4] for line in out.splitlines() if line.strip()]
        if matches != [expected]:
            raise ValueError("Scheduler job identity/owner/workdir does not match this task")
        result = previous | {"job_id": job, "reconciled": True}
        write_json(record, result)
        return result


def monitor(plan, requests):
    from . import baremetal
    if baremetal.enabled(plan):
        return baremetal.monitor(plan, requests)
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.monitor(plan, requests)
    """Read only the exact jobs recorded by this application's controller."""
    result = {}
    for name, request in requests.items():
        if name not in plan["tasks"]:
            raise ValueError("Unknown task")
        job = str(request.get("job_id", ""))
        if not job.isdigit():
            continue
        output = subprocess.check_output(
            ["sacct", "-n", "-P", "-X", "-j", job,
             "--format=JobIDRaw,State,ExitCode,Elapsed,AllocCPUS,ReqMem,Partition,JobName%200,WorkDir%500,User"],
            text=True)
        import getpass
        expected = [f"ah_{plan['identity'][:8]}_{name}_a{request['attempts']}",
                    plan["config"]["output"], getpass.getuser()]
        rows = [line.split("|") for line in output.splitlines() if line.split("|")[0] == job]
        if len(rows) != 1 or rows[0][7:10] != expected:
            result[name] = {"error": "Scheduler identity unavailable or mismatched", "job_id": job}
            continue
        row = rows[0]
        info = dict(zip(("job_id", "state", "exit_code", "elapsed", "cpus", "memory", "partition"), row[:7]))
        log = Path(plan["config"]["output"]) / "logs" / f"{name}_a{request['attempts']}.log"
        if log.is_file():
            with open(log, "rb") as stream:
                stream.seek(max(0, log.stat().st_size - 12000))
                info["log_tail"] = stream.read().decode(errors="replace")
        result[name] = info
    return result


def cancel(plan, requests):
    from . import baremetal
    if baremetal.enabled(plan):
        return baremetal.cancel(plan, requests)
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.cancel(plan, requests)
    # Ownership, workdir and per-attempt job name must all match before scancel.
    observed = monitor(plan, requests)
    if set(observed) != set(requests) or any("error" in info for info in observed.values()):
        raise ValueError("Cannot cancel: exact scheduler identity has not been established")
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}
    for name, info in observed.items():
        if info["state"].split()[0].rstrip("+") not in terminal:
            subprocess.run(["scancel", info["job_id"]], check=True, timeout=30)
    return observed


def cancel_pending(plan, requests):
    from . import baremetal
    if baremetal.enabled(plan):
        return baremetal.cancel(plan, requests, pending_only=True)
    from . import msa_pool
    if msa_pool.enabled(plan):
        return msa_pool.cancel(plan, requests)
    observed = monitor(plan, requests)
    if set(observed) != set(requests) or any("error" in info for info in observed.values()):
        raise ValueError("Cannot reroute: exact scheduler identity has not been established")
    for info in observed.values():
        if info["state"].split()[0].rstrip("+") == "PENDING":
            subprocess.run(["scancel", "--state=PENDING", info["job_id"]], check=True, timeout=30)
    return observed


def msa_pool_drain(plan, request):
    from .msa_pool import drain
    return drain(plan, request)


def msa_pack(plan, request):
    from .msa_transfer import pack
    return pack(plan, request)


def msa_results(plan, request):
    from .msa_transfer import results
    return results(plan, request)


def msa_import(plan, request):
    from .msa_transfer import import_results
    return import_results(plan, request)


def gpu_pack(plan, request):
    from .gpu_transfer import pack
    return pack(plan, request)


def gpu_results(plan, request):
    from .gpu_transfer import results
    return results(plan, request)


def gpu_import(plan, request):
    from .gpu_transfer import import_results
    return import_results(plan, request)


def collect(plan):
    root = Path(plan["config"]["output"])
    target = root / "deliverables.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for base in ("tasks", "logs", "control", "scripts"):
            for path in sorted((root / base).rglob("*")):
                # Keep designed structures, predictions, confidences and logs; exclude MSA search scratch.
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        names = ["plan.json", Path(plan["config"]["input"]).name]
        if plan.get("mode") != "folding":
            names.append("reference.pdb")
        for name in names:
            archive.add(root / name, arcname=name)
    return {"path": str(target), "sha256": digest(target), "bytes": target.stat().st_size}


def load_request(plan, value):
    if not value.startswith("@"):
        return json.loads(value)
    path, expected = value[1:].rsplit("#", 1)
    path = Path(path).resolve()
    directory = (Path(plan["config"]["output"]) / "control" / "requests").resolve()
    if not path.is_relative_to(directory) or digest(path) != expected:
        raise ValueError("Control request path/checksum mismatch")
    return read_json(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=["inspect", "submit", "collect", "reconcile", "monitor", "cancel",
        "cancel_pending", "gpu_pack", "gpu_results", "gpu_import", "msa_pack", "msa_results", "msa_import", "msa_pool_drain"])
    p.add_argument("plan")
    p.add_argument("request", nargs="?", default="{}")
    a = p.parse_args()
    plan = read_json(a.plan)
    result = collect(plan) if a.operation == "collect" else globals()[a.operation](plan, load_request(plan, a.request))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
