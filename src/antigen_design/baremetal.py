"""Durable bare-metal GPU jobs with per-device leases and exact process identity.

Only project-owned journals are managed. External GPU use is observed, never killed.
No Slurm IDs or scheduler acceptance are fabricated.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from .io import read_json, write_json
from .worker import receipt_valid

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes"]


def enabled(plan):
    return bool(plan.get('config', {}).get('execution', {}).get("native_gpu"))


@contextmanager
def locked(path, nonblocking=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield stream


def process_identity(pid):
    proc = Path("/proc") / str(pid)
    try:
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "uid": proc.stat().st_uid, "start_ticks": fields[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "argv": (proc / "cmdline").read_bytes().split(b"\0")[:-1]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def serial_process(pid):
    result = process_identity(pid)
    if result:
        result["argv"] = [value.decode() for value in result["argv"]]
    return result


def alive(identity):
    return bool(identity and identity.get("uid") == os.getuid()
                and serial_process(identity["pid"]) == identity)


def gpu_inventory():
    output = subprocess.check_output(
        ["mthreads-gmi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
         "--format=csv,noheader"], text=True, timeout=20)
    result = []
    for line in output.splitlines():
        cells = [value.strip() for value in line.split(",")]
        if len(cells) != 5:
            raise ValueError("Invalid mthreads-gmi inventory")
        number = lambda value: int(re.fullmatch(r"(\d+)(?: \[[A-Za-z%]+\])?", value)[1])
        result.append({"index": number(cells[0]), "model": cells[1],
                       "total_mb": number(cells[2]), "used_mb": number(cells[3]),
                       "utilization": number(cells[4])})
    if not result or len({row["index"] for row in result}) != len(result):
        raise ValueError("Missing or duplicate GPU inventory")
    return result


def capacity(settings, resources):
    from .gpu_routing import memory_mb
    root = Path(settings["root"])
    leases = root / "device-leases"
    leases.mkdir(parents=True, exist_ok=True)
    memory = {line.split(":")[0]: int(line.split()[1]) // 1024
              for line in Path("/proc/meminfo").read_text().splitlines()}
    available = memory["MemAvailable"]
    cpus = os.cpu_count() or 1
    reserved_cpu = reserved_memory = 0
    idle = []
    for gpu in gpu_inventory():
        if gpu["index"] not in settings["devices"] or gpu["model"] != settings["model"]:
            continue
        path = leases / f"gpu-{gpu['index']}.lock"
        try:
            with locked(path, True):
                # Zero allocation/utilisation also excludes observed external workloads.
                if gpu["used_mb"] == 0 and gpu["utilization"] == 0:
                    idle.append(gpu["index"])
        except BlockingIOError:
            record = read_json(path.with_suffix(".json"))
            reserved_cpu += record["resources"]["cpus"]
            reserved_memory += memory_mb(record["resources"]["memory"])
    free_cpu = max(0, cpus - reserved_cpu)
    free_memory = max(0, min(available, memory["MemTotal"] - reserved_memory))
    slots = min(len(idle), free_cpu // resources["cpus"],
                int(free_memory // memory_mb(resources["memory"])))
    return {"backend": "baremetal", "checked_at": time.time(), "total_gpus": len(settings["devices"]),
            "nodes": [{"node": os.uname().nodename, "gpus": slots, "cpus": free_cpu,
                       "memory_mb": free_memory, "devices": idle[:slots]}] if slots else [],
            "free_gpus": slots}


def record_path(plan, name, attempt):
    if name not in plan["tasks"] or type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid native task/attempt")
    return Path(plan["config"]["output"]) / "control" / f"{name}_a{attempt}.json"


def checked_record(plan, name, attempt, job_id=None):
    record = read_json(record_path(plan, name, attempt))
    if (record["plan_identity"] != plan["identity"] or record["task"] != name
            or record["attempt"] != attempt or record["uid"] != os.getuid()
            or record["root"] != plan["config"]["output"]
            or record["task_signature"] != plan["tasks"][name]["signature"]
            or (job_id is not None and record["job_id"] != job_id)):
        raise ValueError("Native job identity/owner/workdir mismatch")
    return record


def submit(plan, request):
    name, attempt = request["task"], request["attempt"]
    root = Path(plan["config"]["output"])
    path = record_path(plan, name, attempt)
    if request.get("memory_tier", 0) != 0:
        raise ValueError("Native jobs do not change model memory tiers")
    with locked(root / "control" / f"{name}.submit.lock"):
        if path.exists():
            record = checked_record(plan, name, attempt)
            return {key: record[key] for key in ("job_id", "resources", "backend")}
        if (root / "tasks" / name).exists():
            raise ValueError("Refusing to replace existing native task output")
        settings = plan["config"]["execution"]["native_gpu"]
        record = {"job_id": "native-" + uuid.uuid4().hex, "backend": "baremetal",
                  "plan_identity": plan["identity"], "task_signature": plan["tasks"][name]["signature"],
                  "task": name, "attempt": attempt, "uid": os.getuid(), "root": str(root),
                  "state": "SUBMITTING", "created_at": time.time(),
                  "resources": plan["config"]["execution"]["resources"]["gpu"]}
        write_json(path, record)  # durable before Popen; ambiguous launch is never replayed
        for directory in ("logs", "scripts"):
            (root / directory).mkdir(exist_ok=True)
        python = plan["config"]["execution"]["python"][plan["tasks"][name]["stage"]]
        argv = [python, "-m", "antigen_design.baremetal", "supervise", str(root / "plan.json"),
                name, str(attempt), record["job_id"]]
        env = dict(os.environ, PYTHONPATH=plan["config"]["execution"]["source_dir"])
        with (root / "logs" / f"{name}_a{attempt}.log").open("ab") as log:
            subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return {key: record[key] for key in ("job_id", "resources", "backend")}


def duration_seconds(value):
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)", value)
    if not match:
        raise ValueError("Native time limit must be [days-]HH:MM:SS")
    days, hours, minutes, seconds = [int(v or 0) for v in match.groups()]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def supervise(plan, name, attempt, job_id):
    root = Path(plan["config"]["output"])
    path = record_path(plan, name, attempt)
    native = plan["config"]["execution"]["native_gpu"]
    lease_root = Path(native["root"]) / "device-leases"
    lease = child = None
    with locked(root / "control" / f"{name}_a{attempt}.run.lock", True):
        record = checked_record(plan, name, attempt, job_id)
        if record["state"] != "SUBMITTING":
            raise ValueError("Native supervisor already claimed this job")
        record.update(state="PENDING", process=serial_process(os.getpid()))
        write_json(path, record)
        cancel_path = path.with_suffix(".cancel.json")
        def requested():
            if not cancel_path.exists():
                return False
            request = read_json(cancel_path)
            if request != {"job_id": job_id, "plan_identity": plan["identity"]}:
                raise ValueError("Invalid native cancellation identity")
            return True
        try:
            while lease is None:
                # Cancellation and transition out of PENDING share a lock.
                with locked(path.with_suffix(".state.lock")):
                    if requested():
                        record.update(state="CANCELLED", finished_at=time.time(), exit_code=None)
                        write_json(path, record)
                        return
                    with locked(lease_root / "admission.lock"):
                        observed = capacity(native, record["resources"])
                        if observed["nodes"]:
                            device = observed["nodes"][0]["devices"][0]
                            lease_path = lease_root / f"gpu-{device}.lock"
                            lease = open(lease_path, "a")
                            try:
                                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                lease.close()
                                lease = None
                            if lease:
                                write_json(lease_path.with_suffix(".json"),
                                           {"job_id": job_id, "resources": record["resources"],
                                            "root": str(root), "process": record["process"]})
                                record.update(state="RUNNING", gpu_device=device, started_at=time.time())
                                write_json(path, record)
                if lease is None:
                    time.sleep(2)
            stage = plan["tasks"][name]["stage"]
            python = plan["config"]["execution"]["python"][stage]
            env = dict(os.environ, PYTHONPATH=plan["config"]["execution"]["source_dir"],
                       MUSA_VISIBLE_DEVICES=str(device), MTHREADS_VISIBLE_DEVICES=str(device),
                       ANTIGEN_NATIVE_JOB_ID=job_id,
                       OMP_NUM_THREADS=str(record["resources"]["cpus"]))
            child = subprocess.Popen(
                [python, "-m", "antigen_design.worker", str(root / "plan.json"), name,
                 "--attempt", str(attempt)], cwd=root, env=env,
                start_new_session=True, pass_fds=(lease.fileno(),))
            record["worker"] = serial_process(child.pid)
            write_json(path, record)
            deadline = time.monotonic() + duration_seconds(record["resources"].get("time", "02:00:00"))
            stopped = None
            while child.poll() is None:
                stopped = "CANCELLED" if requested() else "TIMEOUT" if time.monotonic() >= deadline else None
                if stopped:
                    # Direct child has not been reaped: PID cannot be recycled.
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=10)
                    break
                time.sleep(1)
            code = child.wait()
            record.update(state=stopped or ("COMPLETED" if code == 0 else "FAILED"),
                          exit_code=code, finished_at=time.time())
            write_json(path, record)
        except Exception as exc:
            if child is not None and child.poll() is None:
                # Preserve UNKNOWN instead of freeing a live worker's lease.
                record.update(state="UNKNOWN", error=str(exc))
            else:
                record.update(state="FAILED", error=str(exc), finished_at=time.time())
            write_json(path, record)
            raise
        finally:
            if lease:
                lease.close()


def monitor(plan, requests):
    result = {}
    for name, request in requests.items():
        record = checked_record(plan, name, request["attempts"], request.get("job_id"))
        state = record["state"]
        if state not in TERMINAL and not alive(record.get("process")):
            state = "UNKNOWN"
        info = {"backend": "baremetal", "job_id": record["job_id"],
                "state": state, "execution_state": state, "slurm_state": None,
                "pid": record.get("process", {}).get("pid"),
                "gpu_device": record.get("gpu_device"), "exit_code": record.get("exit_code"),
                "cpus": record["resources"]["cpus"], "memory": record["resources"]["memory"],
                "partition": None, "elapsed": time.time() - record.get("started_at", record["created_at"])}
        log = Path(record["root"]) / "logs" / f"{name}_a{request['attempts']}.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16000))
                info["log_tail"] = stream.read().decode(errors="replace")
        result[name] = info
    return result


def inspect(plan, requests):
    result = monitor(plan, requests)
    for name, request in requests.items():
        info = result[name]
        if request.get("state") == "submitting":
            info["recovered_job_id"] = info["job_id"]
        directory = Path(plan["config"]["output"]) / "tasks" / name
        if info["state"] == "COMPLETED":
            info["valid"] = receipt_valid(directory, plan["tasks"][name]["signature"])
        if (directory / "error.json").exists():
            info["error"] = read_json(directory / "error.json")
    return result


def cancel(plan, requests, pending_only=False):
    for name, request in requests.items():
        path = record_path(plan, name, request["attempts"])
        with locked(path.with_suffix(".state.lock")):
            record = checked_record(plan, name, request["attempts"], request.get("job_id"))
            if record["state"] in TERMINAL:
                continue
            if not alive(record.get("process")):
                raise ValueError("Native process identity unavailable; no cancellation performed")
            if not pending_only or record["state"] == "PENDING":
                write_json(path.with_suffix(".cancel.json"),
                           {"job_id": record["job_id"], "plan_identity": plan["identity"]})
    return monitor(plan, requests)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["capacity", "supervise"])
    parser.add_argument("payload")
    parser.add_argument("task", nargs="?")
    parser.add_argument("attempt", type=int, nargs="?")
    parser.add_argument("job_id", nargs="?")
    args = parser.parse_args()
    if args.operation == "capacity":
        value = json.loads(args.payload)
        print(json.dumps(capacity(value["native"], value["resources"])))
    else:
        supervise(read_json(args.payload), args.task, args.attempt, args.job_id)


if __name__ == "__main__":
    main()
