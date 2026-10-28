from contextlib import contextmanager
from pathlib import Path
import csv
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from .config import build_plan
from .io import digest, fingerprint, read_json, write_json
from .scheduler import Slurm, command, slurm_script
from .structure import Protein
from .worker import code_signature, probe, receipt_valid
from .stages import ORDER, selected_stages, boundary_complete, check_prerequisites, stage_host, uses_gpu, resource_key

GPU_STAGES = {"rfd3", "mpnn", "fold"}
TERMINAL = {"succeeded", "failed", "blocked"}


def doctor(config, remote=False, stages=None):
    transport = None
    if remote and config["execution"].get("transport") in ("ssh-skill", "openssh"):
        from .transport import get_transport
        transport = get_transport(config)
    report = {"stages": {}, "hosts": {}, "ready": True}
    stages = [s for s in ORDER if (stages is None or s in stages)
              and (s != "rfd3" or config["rfd3"]["enabled"])
              and (config.get("mode") != "folding" or s in ("msa", "fold", "metrics"))]
    for stage in stages:
        from .msa_remote import enabled, target_config
        stage_config = target_config(config) if stage == "msa" and enabled(config) else config
        python = stage_config["execution"]["python"][stage]
        host = stage_host(stage_config, stage) if remote else None
        if not python:
            report["stages"][stage] = {"errors": [f"Configure execution.python.{stage}"]}
            report["ready"] = False
            continue
        # JSON is an argv element and shell-quoted by command(); no shell interpolation.
        code = "import json,sys; from antigen_design.worker import probe; print(json.dumps(probe(sys.argv[1],json.loads(sys.argv[2]))))"
        try:
            argv = [python, "-c", code, stage, json.dumps(stage_config)]
            if stage_config["execution"].get("source_dir"):
                argv = ["env", "PYTHONPATH=" + stage_config["execution"]["source_dir"], *argv]
            timeout = 300 if stage in GPU_STAGES else 90
            output = transport.execute(host, argv, timeout=timeout) if transport else command(argv, host, timeout=timeout)
            data = json.loads(output.splitlines()[-1])
        except subprocess.CalledProcessError as exc:
            data = {"errors": [f"{host or 'local'} {stage} probe exited {exc.returncode}: {(exc.stderr or exc.stdout or '').strip()[-2000:]}"]}
        except Exception as exc:
            data = {"errors": [str(exc)]}
        report["stages"][stage] = data
        if data["errors"]:
            report["ready"] = False
    if remote:
        for host in dict.fromkeys(stage_host(config, s) for s in stages):
            try:
                argv = ["bash", "-lc", "command -v sbatch; sinfo -h -o '%P %a %l %G'"]
                report["hosts"][host] = transport.execute(host, argv) if transport else command(argv, host)
            except Exception as exc:
                report["hosts"][host] = str(exc)
                report["ready"] = False
    if 'msa' in report['stages'] and config['execution'].get('msa_remote',{}).get('enabled') and config['execution']['msa_remote'].get('fallback_when_busy'):
        from copy import deepcopy
        fallback = deepcopy(config);fallback['execution']['msa_remote']['enabled']=False
        checked = doctor(fallback,remote=remote,stages={'msa'})
        report['stages']['msa_remote'] = report['stages']['msa']
        report['stages']['msa'] = checked['stages']['msa']
        report['hosts'].update(checked['hosts']);report['ready'] &= checked['ready']
    return report


def task_graph(plan):
    c, tasks = plan["config"], {}
    def add(name, stage, deps, bi, si=None, seed=None, **extra):
        task = {"id": name, "stage": stage, "deps": deps, "backbone": bi, "sequence": si, "seed": seed,
                "host": stage_host(c, stage), **extra}
        task["signature"] = fingerprint({"task": task, "plan": plan["identity"]})
        tasks[name] = task
    members = []
    if plan.get("mode") == "folding":
        for start in range(0, len(plan["fold_records"]), c["msa"]["batch_size"]):
            batch = [{"record": i} for i in range(start, min(start + c["msa"]["batch_size"], len(plan["fold_records"])))]
            msa = f"msa_batch_{start:04d}"
            add(msa, "msa", [], None, members=batch)
            for member in batch:
                record = plan["fold_records"][member["record"]]
                for seed in record["job"]["seeds"]:
                    fold = f"fold_{record['id']}_seed{seed}"
                    add(fold, "fold", [msa], None, seed=seed, **member)
                    add(f"metrics_{record['id']}_seed{seed}", "metrics", [fold], None, seed=seed, **member)
        return tasks
    for bi, b in enumerate(plan["backbones"]):
        rid, mid = "rfd3_" + b["id"], "mpnn_" + b["id"]
        if c["rfd3"]["enabled"]:
            add(rid, "rfd3", [], bi)
        add(mid, "mpnn", [rid] if c["rfd3"]["enabled"] else [], bi)
        for si in range(c["mpnn"]["sequences"]):
            members.append({"backbone": bi, "sequence": si})
    batch_size = c["msa"].get("batch_size", 1)
    # A batch must not introduce a barrier between independent upstream branches.
    groups = ([members] if c["msa"].get("batch_scope", "all") == "all" else
              [[m for m in members if m["backbone"] == bi] for bi in range(len(plan["backbones"]))])
    batches = [group[start:start + batch_size] for group in groups
               for start in range(0, len(group), batch_size)]
    for batch_index, batch in enumerate(batches):
        first = batch[0]
        if batch_size == 1:
            msa = f"msa_{plan['backbones'][first['backbone']]['id']}_s{first['sequence']:04d}"
            extra = {}
        else:
            msa = f"msa_batch_{batch_index:04d}"
            extra = {"members": batch}
        deps = list(dict.fromkeys("mpnn_" + plan["backbones"][m["backbone"]]["id"] for m in batch))
        add(msa, "msa", deps, first["backbone"], first["sequence"], **extra)
        for member in batch:
            bi, si = member["backbone"], member["sequence"]
            b = plan["backbones"][bi]
            name = f"{b['id']}_s{si:04d}"
            for seed in c["folding"]["seeds"]:
                fold = f"fold_{name}_seed{seed}"
                add(fold, "fold", [msa], bi, si, seed)
                add(f"metrics_{name}_seed{seed}", "metrics", [fold], bi, si, seed)
    return tasks


def initialize(config, dry_run=False, stages=None):
    if config["execution"].get("transport") in ("ssh-skill", "openssh") and config["execution"]["backend"] == "slurm":
        from .remote import initialize as initialize_remote
        return initialize_remote(config, dry_run, stages)
    root = Path(config["output"])
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Output directory is not empty; use resume or a new output: {root}")
    if config["execution"]["backend"] == "slurm" and not root.is_relative_to(Path(config["execution"]["shared_root"])):
        if not dry_run:
            raise ValueError("Slurm controller must run in the shared workspace; see docs/remote.md")
    plan = build_plan(config)
    plan["input_sha256"], plan["code_signature"] = digest(config["input"]), code_signature()
    if not dry_run:
        report = doctor(config, remote=config["execution"]["backend"] == "slurm", stages=stages)
        if not report["ready"]:
            raise ValueError("Dependency preflight failed:\n" + json.dumps(report, indent=2))
        plan["environment"] = report["stages"]
    else:
        plan["environment"] = {}
    plan["dry_run"] = dry_run
    plan["identity"] = fingerprint(plan)
    plan["tasks"] = task_graph(plan)
    root.mkdir(parents=True, exist_ok=True)
    if plan.get("mode") != "folding":
        Protein(config["input"]).normalized(root / "reference.pdb")
        plan["reference_sha256"] = digest(root / "reference.pdb")
    write_json(root / "plan.json", plan)
    write_json(root / "mapping.json", plan["reference_mapping"])
    manifest = {"identity": plan["identity"], "tasks": {name: {"state": "pending", "attempts": 0} for name in plan["tasks"]}}
    write_json(root / "manifest.json", manifest)
    render_scripts(plan)
    summarize(plan, manifest)
    return plan


def render_scripts(plan):
    root, c = Path(plan["config"]["output"]), plan["config"]
    (root / "scripts").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    for name, task in plan["tasks"].items():
        python = c["execution"]["python"][task["stage"]] or f"/CONFIGURE/{task['stage']}/bin/python"
        argv = [python, "-m", "antigen_design.worker", str(root / "plan.json"), name]
        resources = c["execution"]["resources"][resource_key(c, task["stage"])]
        if c["execution"].get("source_dir"):
            argv = ["env", "PYTHONPATH=" + c["execution"]["source_dir"], *argv]
        (root / "scripts" / f"{name}.sh").write_text(slurm_script(argv, root, root / "logs" / f"{name}_%j.log", resources, "ah_" + name))


@contextmanager
def lock_run(root):
    with open(Path(root) / ".controller.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another controller is running") from exc
        yield


def verify_plan(plan):
    if plan["dry_run"]:
        raise ValueError("Dry-run plans cannot execute; start run with a new output directory")
    if code_signature() != plan["code_signature"] or digest(plan["config"]["input"]) != plan["input_sha256"]:
        raise ValueError("Input or pipeline code changed; use a new output directory")
    if plan.get("mode") != "folding" and digest(Path(plan["config"]["output"]) / "reference.pdb") != plan["reference_sha256"]:
        raise ValueError("Normalized reference was modified")


def refresh(plan, manifest, scheduler, retry=False):
    root = Path(plan["config"]["output"])
    for name, task in plan["tasks"].items():
        state = manifest["tasks"][name]
        directory = root / "tasks" / name
        if state["state"] in ("submitted", "submitting"):
            if not state.get("job_id"):
                # Crash between submission and recording job ID: do not resubmit blindly.
                raise RuntimeError(f"Submission outcome unknown for {name}; recover the job ID from Slurm and put it in manifest.json")
            status = scheduler.state(task | state)
            state["slurm_state"] = status
            if status == "COMPLETED":
                state["state"] = "succeeded" if receipt_valid(directory, task["signature"]) else "failed"
                if state["state"] == "failed":
                    state["error"] = "Slurm completed without valid artifacts"
            elif status in ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"):
                state["state"], state["error"] = "failed", status
        elif state["state"] == "running":
            # Local foreground subprocess has returned only if controller was interrupted.
            lock_path = root / ".locks" / name
            if lock_path.exists():
                with open(lock_path, "r") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
            if receipt_valid(directory, task["signature"]):
                state["state"] = "succeeded"
            else:
                state["state"], state["error"] = "failed", "Interrupted local execution"
        if state["state"] == "succeeded" and not receipt_valid(directory, task["signature"]):
            state["state"], state["error"] = "failed", "Artifact checksum mismatch or missing output"
        if state["state"] == "succeeded" and any(manifest["tasks"][d]["state"] != "succeeded" for d in task["deps"]):
            state["state"], state["error"] = "failed", "Upstream artifact invalidated"
        if retry and state["state"] in ("failed", "blocked"):
            state["state"] = "pending"


def advance(plan, manifest, scheduler=None, retry=False, stages=None):
    """One controller pass. All dependency decisions use local verified state."""
    scheduler = scheduler or Slurm()
    c, root = plan["config"], Path(plan["config"]["output"])
    refresh(plan, manifest, scheduler, retry)
    stages = set(ORDER) if stages is None else stages
    check_prerequisites(plan, manifest, stages)
    write_json(root / "manifest.json", manifest)
    for name, task in plan["tasks"].items():
        state = manifest["tasks"][name]
        if state["state"] != "pending" or task["stage"] not in stages:
            continue
        deps = [manifest["tasks"][dep]["state"] for dep in task["deps"]]
        if any(d in ("failed", "blocked") for d in deps):
            state["state"], state["error"] = "blocked", "Upstream stage failed"
            continue
        if not all(d == "succeeded" for d in deps):
            continue
        directory = root / "tasks" / name
        if directory.exists():
            archive = root / "attempts" / f"{name}_{state['attempts']}"
            archive.parent.mkdir(exist_ok=True)
            directory.rename(archive)
        state["attempts"] += 1
        state.pop("error", None)
        if c["execution"]["backend"] == "slurm":
            state["state"] = "submitting"
            state.pop("job_id", None)
            state.pop("slurm_state", None)
            write_json(root / "manifest.json", manifest)
            # If sbatch response is lost, retain submitting state to prevent duplicate jobs.
            state["job_id"] = scheduler.submit(task, root / "scripts" / f"{name}.sh")
            state["state"] = "submitted"
        else:
            state["state"] = "running"
            write_json(root / "manifest.json", manifest)
            argv = [c["execution"]["python"][task["stage"]], "-m", "antigen_design.worker", str(root / "plan.json"), name]
            with open(root / "logs" / f"{name}_{state['attempts']}.log", "w") as log:
                completed = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
            state["state"] = "succeeded" if completed.returncode == 0 and receipt_valid(directory, task["signature"]) else "failed"
            if state["state"] == "failed":
                error = directory / "error.json"
                state["error"] = read_json(error)["error"] if error.exists() else f"Worker exit {completed.returncode} without valid artifacts"
        write_json(root / "manifest.json", manifest)
    write_json(root / "manifest.json", manifest)
    summarize(plan, manifest)
    return boundary_complete(plan, manifest, stages)


def summarize(plan, manifest):
    root = Path(plan.get("local_output", plan["config"]["output"]))
    rows = []
    for name, task in plan["tasks"].items():
        if task["stage"] != "metrics":
            continue
        state = manifest["tasks"][name]
        if state["state"] == "succeeded":
            rows.extend(read_json(root / "tasks" / name / "metrics.json"))
        elif plan.get("mode") == "folding":
            record = plan["fold_records"][task["record"]]
            for sample in range(plan["config"]["folding"]["samples"]):
                rows.append({"design": record["id"], "name": record["job"]["display_name"],
                             "fold_seed": task["seed"], "sample": sample,
                             "status": state["state"], "error": state.get("error", "")})
        else:
            b = plan["backbones"][task["backbone"]]
            for sample in range(plan["config"]["folding"]["samples"]):
                rows.append({"design": f"{b['id']}_s{task['sequence']:04d}", "backbone": b["id"],
                    "design_seed": b["seed"], "fold_seed": task["seed"], "sample": sample,
                    "length": len(b["mapping"]), "status": state["state"], "error": state.get("error", "")})
    keys = list(dict.fromkeys(key for row in rows for key in row))
    temp = root / "summary.csv.tmp"
    with open(temp, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows({k: "NA" if v is None else v for k, v in row.items()} for row in rows)
    os.replace(temp, root / "summary.csv")


def resume(root, wait=False, retry=False, poll=15, until=None, only=None):
    stages = selected_stages(until, only)
    root = Path(root).resolve()
    if "local_output" in read_json(root / "plan.json"):
        from .remote import resume as resume_remote
        return resume_remote(root, wait, retry, poll, until, only)
    with lock_run(root):
        plan, manifest = read_json(root / "plan.json"), read_json(root / "manifest.json")
        active_stages = {task["stage"] for task in plan["tasks"].values()}
        if only and only not in active_stages:
            raise ValueError(f"Stage {only} is not enabled in this plan")
        stages &= active_stages
        verify_plan(plan)
        report = doctor(plan["config"], remote=plan["config"]["execution"]["backend"] == "slurm", stages=stages)
        if not report["ready"] or any(s in plan["environment"] and e != plan["environment"][s] for s, e in report["stages"].items()):
            raise ValueError("Environment no longer matches plan; run doctor and start a new output directory")
        plan["environment"].update(report["stages"])
        write_json(root / "plan.json", plan)
        while True:
            done = advance(plan, manifest, retry=retry, stages=stages)
            retry = False
            counts = {}
            for state in manifest["tasks"].values():
                counts[state["state"]] = counts.get(state["state"], 0) + 1
            print(json.dumps(counts), flush=True)
            if done or not wait:
                return manifest
            time.sleep(min(30, max(1, poll)))
