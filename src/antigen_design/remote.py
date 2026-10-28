"""WSL controller with local manifests and server-validated remote artifacts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tarfile
import time
import uuid
import tempfile
from .config import build_plan
from .io import digest, fingerprint, read_json, write_json
from .structure import Protein
from .transport import get_transport
from .worker import code_signature
from .stages import ORDER, selected_stages, check_prerequisites, boundary_complete, uses_gpu

ACTIVE = {"submitting", "submitted", "running"}
FAILED = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


def deploy_code(config):
    """Content-addressed immutable source; old running jobs keep their original code."""
    transport = get_transport(config)
    host = config["execution"]["cpu_host"]
    release = Path(config["execution"]["remote_root"]) / "releases" / code_signature()
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "source.tar"
        with tarfile.open(bundle, "w") as archive:
            for path in sorted(Path(__file__).parent.glob("*.py")):
                archive.add(path, arcname="src/antigen_design/" + path.name)
        transport.execute(host, ["mkdir", "-p", str(release)])
        transport.upload(host, bundle, release / "source.tar")
        transport.execute(host, ["tar", "-xf", str(release / "source.tar"), "-C", str(release)])
    return str(release / "src")


def initialize(config, dry_run=False, stages=None):
    from .pipeline import doctor, task_graph, summarize
    root = Path(config["output"])
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Output directory is not empty: {root}")
    plan = build_plan(config)
    remote_config = deepcopy(config)
    run_id = root.name + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    remote_root = Path(config["execution"]["remote_root"]) / "runs" / run_id
    remote_config["output"] = str(remote_root)
    input_name = "input" + Path(config["input"]).suffix.lower()
    remote_config["input"] = str(remote_root / input_name)
    if not dry_run:
        remote_config["execution"]["source_dir"] = deploy_code(config)
        from .msa_remote import enabled, deploy
        if enabled(remote_config):
            deploy(remote_config, root)
    plan.update(config=remote_config, local_output=str(root), local_input=config["input"],
                input_sha256=digest(config["input"]), code_signature=code_signature(), dry_run=dry_run)
    if dry_run:
        plan["environment"] = {}
    else:
        report = doctor(remote_config, remote=True, stages=stages)
        if not report["ready"]:
            raise ValueError("Remote preflight failed:\n" + json.dumps(report, indent=2))
        plan["environment"] = report["stages"]
        if any(e["code"] != plan["code_signature"] for e in plan["environment"].values()):
            raise ValueError("Remote pipeline source differs from local source; deploy the current package")
    plan["identity"] = fingerprint(plan)
    plan["tasks"] = task_graph(plan)
    root.mkdir(parents=True, exist_ok=True)
    if plan.get("mode") != "folding":
        Protein(config["input"]).normalized(root / "reference.pdb")
        plan["reference_sha256"] = digest(root / "reference.pdb")
    write_json(root / "plan.json", plan)
    write_json(root / "mapping.json", plan["reference_mapping"])
    manifest = {"identity": plan["identity"], "tasks": {name: {"state": "pending", "attempts": 0,
                "memory_tier": 0, "host": task["host"], "stage": task["stage"], "history": []} for name, task in plan["tasks"].items()}}
    write_json(root / "manifest.json", manifest)
    summarize(plan, manifest)
    if not dry_run:
        transport = get_transport(config)
        host = config["execution"]["cpu_host"]
        transport.execute(host, ["mkdir", "-p", str(remote_root / "logs"), str(remote_root / "scripts"), str(remote_root / "control")])
        bundle = root / "input_bundle.tar"
        with tarfile.open(bundle, "w") as archive:
            files = [(config["input"], input_name), (root / "plan.json", "plan.json")]
            if plan.get("mode") != "folding":
                files.append((root / "reference.pdb", "reference.pdb"))
            for source, name in files:
                archive.add(source, arcname=name)
        transport.upload(host, bundle, remote_root / bundle.name)
        transport.execute(host, ["tar", "-xf", str(remote_root / bundle.name), "-C", str(remote_root)])
        paths = [remote_config["input"]] + ([str(remote_root / "reference.pdb")] if plan.get("mode") != "folding" else [])
        evidence = transport.execute(host, ["sha256sum", *paths])
        if plan["input_sha256"] not in evidence or (plan.get("mode") != "folding" and plan["reference_sha256"] not in evidence):
            raise ValueError("Uploaded structure hash mismatch")
        write_json(root / "upload_verification.json", {"sha256sum": evidence})
    return plan


def control(transport, plan, host, operation, request=None):
    if operation in ("inspect", "monitor", "cancel", "cancel_pending") and any(
            state.get("route") for state in (request or {}).values()):
        result = {}
        for name, state in request.items():
            compact = {k: state[k] for k in ("state", "job_id", "attempts", "memory_tier") if k in state}
            if not state.get("route"):
                result.update(control_raw(transport, plan, host, operation, {name: compact}))
                continue
            route = state["route"]
            observed = control_raw(transport, route["plan"], state["host"], operation, {name: compact})
            if operation == "inspect" and observed[name].get("valid"):
                if not state.get("imported"):
                    if route.get("kind") == "msa":
                        from .msa_remote import returned
                        if not returned(plan, state, name, transport):
                            observed[name]["transfer_pending"] = True
                            result.update(observed)
                            continue
                    else:
                        from .gpu_routing import returned
                        returned(plan, state, name, transport)
                checked = control_raw(transport, plan, plan["config"]["execution"]["cpu_host"],
                    "inspect", {name: dict(compact, state="succeeded", job_id=None)})
                observed[name]["valid"] = checked[name].get("valid", False)
            result.update(observed)
        return result
    return control_raw(transport, plan, host, operation, request)


def control_raw(transport, plan, host, operation, request=None):
    python = plan["config"]["execution"]["python"]["metrics"]
    payload = json.dumps(request or {})
    # Windows SSH command lines have a small length limit. Batch design capsules
    # can exceed it; transmit a scoped, checksummed request file instead.
    if len(payload.encode()) > 8192:
        directory = Path(plan["config"]["output"]) / "control" / "requests"
        transport.execute(host, ["mkdir", "-p", str(directory)])
        with tempfile.TemporaryDirectory(dir=plan.get("local_output")) as temp:
            path = Path(temp) / (uuid.uuid4().hex + ".json")
            path.write_text(payload)
            remote_path = directory / path.name
            transport.upload(host, path, remote_path)
            payload = "@" + str(remote_path) + "#" + digest(path)
    argv = [python, "-m", "antigen_design.remote_control", operation,
            str(Path(plan["config"]["output"]) / "plan.json"), payload]
    if plan["config"]["execution"].get("source_dir"):
        argv = ["env", "PYTHONPATH=" + plan["config"]["execution"]["source_dir"], *argv]
    result = transport.execute(host, argv, timeout=90)
    return json.loads(result.splitlines()[-1])


def is_gpu_oom(info):
    error = info.get("error", {})
    text = (error.get("error", "") + error.get("traceback", "") + info.get("log_tail", "")).lower()
    return "cuda out of memory" in text or "cuda error: out of memory" in text or "torch.outofmemoryerror" in text


def refresh(plan, manifest, transport):
    root = Path(plan["local_output"])
    groups = {}
    for name, state in manifest["tasks"].items():
        if state["state"] in ACTIVE | {"succeeded"}:
            groups.setdefault(state["host"], {})[name] = state
    for host, requests in groups.items():
        observed = control(transport, plan, host, "inspect", requests)
        for name, info in observed.items():
            state = manifest["tasks"][name]
            state["slurm_state"] = info["slurm_state"]
            execution_state = info.get("execution_state", info["slurm_state"])
            if info.get('backend') == 'baremetal':
                state.update(backend='baremetal', execution_state=execution_state)
            for key in ("pool", "pool_state", "pool_phase"):
                if key in info:
                    state[key] = info[key]
            if info.get("transfer_pending"):
                state["state"] = "running"
                write_json(root / "manifest.json", manifest)
                continue
            from .gpu_routing import finish_cancel
            if finish_cancel(state, execution_state):
                continue
            if state["state"] == "submitting":
                if info.get("recovered_job_id"):
                    state.update(job_id=info["recovered_job_id"], state="submitted")
                else:
                    raise RuntimeError(f"Submission outcome unknown for {name}; inspect its remote control record and Slurm job name")
            if execution_state == "COMPLETED" or state["state"] == "succeeded":
                state["state"] = "succeeded" if info.get("valid") else "failed"
                if state["state"] == "failed":
                    state["error"] = "Missing or corrupted remote artifacts"
                elif "metrics" in info:
                    write_json(root / "tasks" / name / "metrics.json", info["metrics"])
            elif execution_state in FAILED:
                state.update(state="failed", error=info.get("error", {}).get("error") or execution_state)
                write_json(root / "failures" / f"{name}_a{state['attempts']}.json", info)
            if execution_state == "RUNNING":
                state["state"] = "running"
            if execution_state == "PENDING" and not state.get("queue_reported") and time.time() - state.get("submitted_at", time.time()) >= plan["config"]["execution"]["queue_notice_seconds"]:
                print(f"Queue notice: {name} job {state.get('job_id')} has waited at least two hours; keeping the existing job.", flush=True)
                state["queue_reported"] = True
    # A previously successful downstream result cannot survive invalidation of its prerequisite.
    for name, task in plan["tasks"].items():
        state = manifest["tasks"][name]
        if state["state"] == "succeeded" and any(manifest["tasks"][d]["state"] != "succeeded" for d in task["deps"]):
            state.update(state="failed", error="Upstream artifact invalidated")


def advance(plan, manifest, transport, retry=False, stages=None):
    from .runtime_limits import admission
    with admission(plan) as (limits, other_active):
        return _advance(plan, manifest, transport, retry, stages, limits, other_active)


def _advance(plan, manifest, transport, retry, stages, limits, other_active):
    from .pipeline import GPU_STAGES, TERMINAL, summarize
    root = Path(plan["local_output"])
    refresh(plan, manifest, transport)
    routing = plan["config"]["execution"].get("gpu_routing", {}).get("enabled", False)
    if routing:
        from .gpu_routing import reroute_pending
        reroute_pending(plan, manifest, transport)
    stages = set(ORDER) if stages is None else stages
    if retry:
        for state in manifest["tasks"].values():
            if state["state"] in ("failed", "blocked"):
                state["state"] = "pending"
    write_json(root / "manifest.json", manifest)
    check_prerequisites(plan, manifest, stages)
    manifest["admission_limits"] = {k: limits[k] for k in ("max_gpu_jobs", "max_msa_jobs")}
    # Drain ready downstream work before filling GPUs with more upstream branches.
    # Dependencies below remain the sole readiness authority, including receipts.
    priority = {"metrics": 0, "msa": 1, "fold": 2, "mpnn": 3, "rfd3": 4}
    ordered = sorted(plan["tasks"].items(), key=lambda item: priority.get(item[1]["stage"], 5))
    for name, task in ordered:
        if (root.parent / "cancel.request").exists():
            break
        state = manifest["tasks"][name]
        if state["state"] != "pending" or task["stage"] not in stages:
            continue
        policy = plan["config"]["execution"].get("runtime_limits_file")
        if policy:
            from .runtime_limits import validate
            limits = validate(read_json(policy))
            manifest["admission_limits"] = limits
        deps = [manifest["tasks"][d]["state"] for d in task["deps"]]
        if any(s in ("failed", "blocked") for s in deps):
            state.update(state="blocked", error="Upstream stage failed")
            continue
        if not all(s == "succeeded" for s in deps):
            continue
        active = other_active + [s for s in manifest["tasks"].values() if s["state"] in ACTIVE]
        if limits["max_gpu_jobs"] and uses_gpu(plan["config"], task["stage"]) and sum(uses_gpu(plan["config"], s["stage"]) for s in active) >= limits["max_gpu_jobs"]:
            continue
        if task["stage"] == "msa" and sum(s["stage"] == "msa" for s in active) >= limits["max_msa_jobs"]:
            continue
        if task["stage"] == "msa" and plan["config"]["execution"].get("msa_remote", {}).get("enabled"):
            from .msa_remote import stage as msa_stage
            from .msa_routing import choose
            target = choose(plan,manifest,name,transport)
            if target is None:
                continue
            if target == 'remote':
                msa_stage(plan, manifest, name, transport)
            else:
                state['host'] = plan['config']['execution']['cpu_host']
        if routing and task["stage"] in ("fold", "rfd3", "mpnn"):
            from .gpu_routing import choose, stage
            selected = choose(plan, manifest, transport,
                exclude=state.get("avoid_target") if time.time() < state.get("avoid_until", 0) else None,
                stage_name=task["stage"])
            if selected is None:
                state["routing_reason"] = "等待兼容的空闲 GPU；定期重查所有候选集群"
                continue
            stage(plan, manifest, name, transport, selected)
        if (root.parent / "cancel.request").exists():
            break
        state.update(state="submitting", attempts=state["attempts"] + 1)
        state.pop("job_id", None)
        state.pop("error", None)
        write_json(root / "manifest.json", manifest)
        execution_plan = state["route"]["plan"] if state.get("route") else plan
        result = control(transport, execution_plan, state["host"], "submit", {"task": name, "attempt": state["attempts"], "memory_tier": state["memory_tier"]})
        if result.get("rejected"):
            state.update(state="failed", error="sbatch rejected submission: " + result["rejected"])
        else:
            state.update(result, state="submitted", submitted_at=time.time(), queue_reported=False)
        write_json(root / "manifest.json", manifest)
    write_json(root / "manifest.json", manifest)
    summarize(plan, manifest)
    return boundary_complete(plan, manifest, stages)


def resume(root, wait=False, retry=False, poll=30, until=None, only=None):
    from .pipeline import lock_run, doctor
    root = Path(root).resolve()
    stages = selected_stages(until, only)
    with lock_run(root):
        plan, manifest = read_json(root / "plan.json"), read_json(root / "manifest.json")
        active_stages = {task["stage"] for task in plan["tasks"].values()}
        if only and only not in active_stages:
            raise ValueError(f"Stage {only} is not enabled in this plan")
        stages &= active_stages
        if plan["dry_run"]:
            raise ValueError("Dry-run plans cannot execute")
        if code_signature() != plan["code_signature"] or digest(plan["local_input"]) != plan["input_sha256"]:
            raise ValueError("Local input or implementation changed since planning")
        report = doctor(plan["config"], remote=True, stages=stages)
        if not report["ready"] or any(s in plan["environment"] and e != plan["environment"][s] for s, e in report["stages"].items()):
            raise ValueError("Remote environment changed since preflight: " + json.dumps(report))
        transport = get_transport(plan["config"])
        if any(s not in plan["environment"] for s in stages):
            if any(t["state"] in ACTIVE for t in manifest["tasks"].values()):
                raise ValueError("Finish active stages before extending stage environment records")
            plan["environment"].update(report["stages"])
            write_json(root / "plan.json", plan)
            remote_plan = Path(plan["config"]["output"]) / "plan.json"
            transport.upload(plan["config"]["execution"]["cpu_host"], root / "plan.json", str(remote_plan) + ".tmp")
            transport.execute(plan["config"]["execution"]["cpu_host"], ["mv", str(remote_plan) + ".tmp", str(remote_plan)])
        while True:
            done = advance(plan, manifest, transport, retry, stages=stages)
            retry = False
            counts = {}
            for state in manifest["tasks"].values():
                counts[state["state"]] = counts.get(state["state"], 0) + 1
            print(json.dumps(counts), flush=True)
            if done or not wait:
                return manifest
            time.sleep(min(30, max(1, poll)))


def collect(root):
    root = Path(root).resolve()
    plan = read_json(root / "plan.json")
    transport = get_transport(plan["config"])
    archive = control(transport, plan, plan["config"]["execution"]["cpu_host"], "collect")
    target = root / "deliverables.tar.gz"
    transport.download(plan["config"]["execution"]["cpu_host"], archive["path"], target)
    if digest(target) != archive["sha256"]:
        raise ValueError("Downloaded deliverable checksum mismatch")
    write_json(root / "download_verification.json", archive)
    return target


def reconcile(root, task_id, job_id):
    from .pipeline import lock_run
    root = Path(root).resolve()
    with lock_run(root):
        plan, manifest = read_json(root / "plan.json"), read_json(root / "manifest.json")
        state = manifest["tasks"][task_id]
        if state["state"] != "submitting" or plan["dry_run"]:
            raise ValueError("Only an ambiguous real submission can be reconciled")
        execution_plan = state["route"]["plan"] if state.get("route") else plan
        result = control(get_transport(plan["config"]), execution_plan, state["host"], "reconcile",
                         {"task": task_id, "job_id": job_id, "attempt": state["attempts"]})
        state.update(result, state="submitted")
        write_json(root / "manifest.json", manifest)
        return state
