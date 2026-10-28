"""Web orchestration with scoped live preflight; frozen workers remain unchanged."""
import argparse
import fcntl
import os
import subprocess
import time
import traceback
from antigen_design.io import read_json, write_json, digest
from antigen_design.worker import code_signature
from antigen_design.web_store import Store
from antigen_design.web_runner import status, observe
from .collection import extract_results
from .preflight import checked_preflight


def run(store, job):
    from ..pipeline import initialize, lock_run
    from ..remote import advance, refresh, control, ACTIVE
    from ..transport import get_transport
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
                from .initialization import prepare
                config = prepare(config, root)
                initialize(config)
            if not (pipeline / "manifest.json").exists():
                raise ValueError("Initialization incomplete: manifest missing; preserve this run and inspect controller.log before creating a new run")
            plan = read_json(pipeline / "plan.json")
            cancelling = (root / "cancel.request").exists()
            if plan["dry_run"] or (not cancelling and (
                    plan["code_signature"] != code_signature() or digest(plan["local_input"]) != plan["input_sha256"])):
                raise ValueError("Source/input identity changed; create a new run")
            stages = {t["stage"] for t in plan["tasks"].values()}
            transport = get_transport(plan["config"])
            with lock_run(pipeline):
                manifest = read_json(pipeline / "manifest.json")
                if (not cancelling and manifest['tasks']
                        and set(manifest['tasks']) == set(plan['tasks'])
                        and all(s['state'] == 'succeeded' for s in manifest['tasks'].values())):
                    # No computation is eligible. Collection validates all remote
                    # receipts and artifacts; unavailable retired engines need no probe.
                    status(root, 'collecting')
                    extract_results(root, plan)
                    status(root, 'succeeded', verification='remote receipts + archive hash + collected artifact hashes')
                    return
                if not initialized and not cancelling:
                    report = checked_preflight(plan, manifest, transport)
                    write_json(root / 'preflight.json', report)
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
