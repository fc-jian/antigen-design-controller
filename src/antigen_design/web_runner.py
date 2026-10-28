"""Durable web-run controller; uses the repository's audited remote stage engine."""
import argparse
import fcntl
import os
import subprocess
import tarfile
import time
import traceback
from pathlib import Path
from .io import read_json, write_json, digest
from .web_store import Store, now
from .worker import code_signature


def status(root, state, **extra):
    old = read_json(root / "status.json")
    write_json(root / "status.json", old | {"state": state, "updated_at": now(), **extra})


def observe(root, plan, manifest, transport):
    from .remote import control
    groups, observations = {}, {}
    for name, state in manifest["tasks"].items():
        if state.get("job_id"):
            groups.setdefault(state["host"], {})[name] = state
    try:
        for host, requests in groups.items():
            observations.update(control(transport, plan, host, "monitor", requests))
        write_json(root / "monitor.json", {"observed_at": now(), "tasks": observations})
    except Exception as exc:
        old = read_json(root / "monitor.json") if (root / "monitor.json").exists() else {}
        write_json(root / "monitor.json", old | {"error": str(exc), "attempted_at": now()})


def extract_results(root, plan):
    from .remote import collect
    from .folding_results import cif_residues, summary
    pipeline = root / "pipeline"
    archive_path = collect(pipeline)
    target = root / "collected"
    target.mkdir(exist_ok=True)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 2 * 1024**3:
            raise ValueError("Results exceed 2 GiB local collection limit")
        for member in members:
            path = (target / member.name).resolve()
            if not path.is_relative_to(target.resolve()) or not member.isfile():
                raise ValueError("Unsafe result archive member")
        for member in members:
            path = target / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, open(path, "wb") as dest:
                import shutil
                shutil.copyfileobj(source, dest)
    remote = Path(plan["config"]["output"])
    # Validate each collected internal artifact against its worker receipt.
    for receipt_path in target.glob("tasks/*/complete.json"):
        receipt = read_json(receipt_path)
        task = plan["tasks"][receipt_path.parent.name]
        if receipt["signature"] != task["signature"]:
            raise ValueError("Collected receipt signature mismatch")
        for path, expected in receipt["artifacts"].items():
            source = Path(path)
            if source.is_relative_to(remote):
                local = target / source.relative_to(remote)
                if not local.is_file() or digest(local) != expected:
                    raise ValueError(f"Collected artifact checksum mismatch: {local.name}")
    results = []
    for metric_path in sorted(target.glob("tasks/metrics_*/metrics.json")):
        for row in read_json(metric_path):
            prediction = target / Path(row["prediction"]).relative_to(remote)
            confidence = target / Path(row["confidence_path"]).relative_to(remote)
            residues = cif_residues(prediction)
            full = sorted(prediction.parent.glob(f"*full_data*sample_{row['sample']}.json"))
            # Protenix versions have used both full_data and full_confidence.
            if not full:
                full = sorted(prediction.parent.glob(f"*full_confidence*sample_{row['sample']}.json"))
            item = {"id": len(results), "name": row.get("name", row["design"]), "seed": row["fold_seed"],
                    "sample": row["sample"], "metrics": row, "confidence": summary(confidence),
                    "residues": residues, "structure": str(prediction.relative_to(root)),
                    "summary_file": str(confidence.relative_to(root)), "pae": None, "pae_labels": None,
                    "plddt_source": "predicted CIF atom B_iso_or_equiv; residue mean"}
            if len(full) == 1 and full[0].stat().st_size < 64 * 1024**2:
                data = read_json(full[0])
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                if isinstance(data, dict):
                    item["full_confidence_file"] = str(full[0].relative_to(root))
                    pae = data.get("pae", data.get("token_pair_pae"))
                    if isinstance(pae, list) and 0 < len(pae) <= 2000 and all(isinstance(r, list) and len(r) == len(pae) for r in pae):
                        import math
                        if all(isinstance(v, (float, int)) and math.isfinite(v) and v >= 0 for r in pae for v in r):
                            item["pae"] = pae
                            ids, positions = data.get("token_chain_ids"), data.get("token_res_ids")
                            if isinstance(ids, list) and isinstance(positions, list) and len(ids) == len(positions) == len(pae):
                                item["pae_labels"] = [f"{c}:{p}" for c, p in zip(ids, positions)]
            write_json(root / f"result-{item['id']}.json", item)
            results.append({k: v for k, v in item.items() if k not in ("residues", "pae", "pae_labels")})
    if len(results) != plan["expected_predictions"]:
        raise ValueError("Collected prediction count differs from plan")
    write_json(root / "results.json", results)


def run(store, job):
    from .pipeline import initialize, lock_run, doctor
    from .remote import advance, refresh, control, ACTIVE
    from .transport import get_transport
    root = store.job_root(job)
    with open(root / ".runner.lock", "w") as runner_lock:
        try:
            fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            status(root, "starting", error=None, controller_pid=os.getpid())
            pipeline = root / "pipeline"
            if (root / "cancel.request").exists() and not (pipeline / "plan.json").exists():
                status(root, "cancelled")
                return
            initialized = not (pipeline / "plan.json").exists()
            if initialized:
                config = read_json(store.root / "configs" / (job + ".json"))
                initialize(config)
            if not (pipeline / "manifest.json").exists():
                raise ValueError("Initialization incomplete: manifest missing; preserve this run and inspect controller.log before creating a new run")
            plan = read_json(pipeline / "plan.json")
            cancelling = (root / "cancel.request").exists()
            if plan["dry_run"] or (not cancelling and (
                    plan["code_signature"] != code_signature() or digest(plan["local_input"]) != plan["input_sha256"])):
                raise ValueError("Source/input identity changed; create a new run")
            stages = {t["stage"] for t in plan["tasks"].values()}
            if not initialized and not cancelling:
                preflight = doctor(plan["config"], remote=True, stages=stages)
                if not preflight["ready"] or preflight["stages"] != plan["environment"]:
                    raise ValueError("Environment identity differs from the frozen run")
            transport = get_transport(plan["config"])
            with lock_run(pipeline):
                manifest = read_json(pipeline / "manifest.json")
                retry = (root / "retry.request").exists()
                if retry:
                    refresh(plan, manifest, transport)
                    if any(s["state"] in ACTIVE for s in manifest["tasks"].values()):
                        raise ValueError("Active or ambiguous jobs must resolve before retry")
                    (root / "retry.request").unlink()
                errors = 0
                while True:
                    cancelling = (root / "cancel.request").exists()
                    if not cancelling and (plan["code_signature"] != code_signature() or digest(plan["local_input"]) != plan["input_sha256"]):
                        raise ValueError("Source/input changed while run was active")
                    try:
                        if cancelling:
                            status(root, "cancelling")
                            refresh(plan, manifest, transport)
                            groups = {}
                            for name, state in manifest["tasks"].items():
                                if state["state"] in ACTIVE:
                                    if not state.get("job_id"):
                                        raise ValueError("Unknown submission outcome; resolve before cancellation")
                                    groups.setdefault(state["host"], {})[name] = state
                            for host, requests in groups.items():
                                control(transport, plan, host, "cancel", requests)
                            write_json(pipeline / "manifest.json", manifest)
                            observe(root, plan, manifest, transport)
                            if not groups:
                                for state in manifest["tasks"].values():
                                    if state["state"] == "pending":
                                        state.update(state="blocked", error="Cancelled by application user")
                                write_json(pipeline / "manifest.json", manifest)
                                status(root, "cancelled")
                                return
                        else:
                            status(root, "running")
                            done = advance(plan, manifest, transport, retry=retry, stages=stages)
                            retry = False
                            observe(root, plan, manifest, transport)
                            if done:
                                if all(t["state"] == "succeeded" for t in manifest["tasks"].values()):
                                    status(root, "collecting")
                                    extract_results(root, plan)
                                    status(root, "succeeded", verification="remote receipts + archive hash + collected artifact hashes")
                                else:
                                    status(root, "failed", error="One or more stages failed; inspect task logs")
                                return
                        errors = 0
                    except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
                        if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                            raise
                        if any(s["state"] == "submitting" for s in manifest["tasks"].values()):
                            raise  # ambiguous submissions require explicit recovery, never blind retries
                        errors += 1
                        print(f"Connection failure {errors}/3: {exc}", flush=True)
                        if errors >= 3:
                            raise
                    time.sleep(15)
        except Exception as exc:
            traceback.print_exc()
            status(root, "attention", error=str(exc))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project", default="webapp")
    parser.add_argument("job")
    args = parser.parse_args()
    run(Store(args.repo, args.project), args.job)


if __name__ == "__main__":
    main()
