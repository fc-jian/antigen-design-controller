"""Independent stage process: atomic completion receipts and explicit environment checks."""
import argparse
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import traceback
import fcntl
from .io import digest, fingerprint, read_json, write_json


def code_signature():
    return fingerprint({p.name: digest(p) for p in Path(__file__).parent.glob("*.py")})


def configure_environment(config):
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    cache = Path(config["execution"].get("remote_root") or config["output"]) / "runtime-cache"
    for variable, subdirectory in {
        "XDG_CACHE_HOME": "xdg", "TORCH_HOME": "torch", "TORCH_EXTENSIONS_DIR": "torch_extensions",
        "TRITON_CACHE_DIR": "triton", "HF_HOME": "huggingface", "MPLCONFIGDIR": "matplotlib",
        "CUDA_CACHE_PATH": "cuda", "NUMBA_CACHE_DIR": "numba",
    }.items():
        os.environ[variable] = str(cache / subdirectory)
    os.environ["WANDB_MODE"] = "disabled"
    os.environ["FOUNDRY_CHECKPOINT_DIRS"] = config["foundry_weights"]
    if config["folding"].get("data_root"):
        os.environ["PROTENIX_ROOT_DIR"] = config["folding"]["data_root"]


def probe(stage, config):
    configure_environment(config)
    packages = ["numpy", "biopython", "PyYAML"]
    required = {"rfd3": ["rc-foundry", "torch", "rfd3"], "mpnn": ["rc-foundry", "torch", "mpnn"],
                "msa": ["colabfold"], "fold": ["protenix", "torch"]}.get(stage, [])
    errors, versions, resources = [], {}, {}
    # Foundry model distributions can use different names; import presence is authoritative.
    for package in packages + required:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
            if package not in ("rfd3", "mpnn") and not (package == 'protenix' and config['execution'].get('native_gpu')):
                errors.append(f"Missing package: {package}")
    for module in {"rfd3": ["rfd3", "lightning"], "mpnn": ["mpnn"], "fold": ["runner", "protenix"], "msa": ["colabfold"]}.get(stage, []):
        spec = importlib.util.find_spec(module)
        if spec is None:
            errors.append(f"Missing module: {module}")
        elif spec.origin and Path(spec.origin).is_file():
            resources["module:" + module] = digest(spec.origin)
            if spec.submodule_search_locations:
                resources["source:" + module] = fingerprint({str(p.relative_to(base)): digest(p)
                    for location in spec.submodule_search_locations for base in [Path(location)]
                    for p in sorted(base.rglob("*.py"))})
    def file(path, label, full_hash=False):
        if not path or not Path(path).is_file():
            errors.append(f"Missing {label}: {path}")
            return
        p = Path(path)
        resources[label] = {"path": str(p.resolve()), "size": p.stat().st_size,
                            "mtime_ns": p.stat().st_mtime_ns}
        if full_hash:
            resources[label]["sha256"] = digest(p)
    if config['execution'].get('native_gpu'):
        file(config['execution']['python'][stage], 'launcher', True)
    if stage in ("rfd3", "mpnn"):
        os.environ["FOUNDRY_CHECKPOINT_DIRS"] = config["foundry_weights"]
        checkpoint = config[stage]["checkpoint"]
        if checkpoint in ("rfd3", "proteinmpnn"):
            try:
                from foundry.inference_engines.checkpoint_registry import REGISTERED_CHECKPOINTS
                checkpoint = str(REGISTERED_CHECKPOINTS[checkpoint].get_default_path())
            except Exception as exc:
                errors.append(f"Cannot resolve Foundry checkpoint: {exc}")
                checkpoint = None
        file(checkpoint, "checkpoint")
        if checkpoint and Path(checkpoint).is_file() and not Path(checkpoint).resolve().is_relative_to(Path(config["foundry_weights"]).resolve()):
            errors.append("Resolved Foundry checkpoint is outside the supplied weight directory")
        if stage == "rfd3":
            try:
                import inspect
                from rfd3.engine import RFD3InferenceConfig, RFD3InferenceEngine
                inspect.signature(RFD3InferenceEngine).bind(**RFD3InferenceConfig())
            except Exception as exc:
                errors.append(f"Incompatible RFD3 engine/config interface: {exc}")
    if stage == "msa":
        file(config["msa"]["script"], "converter", True)
        for name in ("colabfold_search", config["msa"]["mmseqs"]):
            file(shutil.which(name), "executable:" + name, True)
        db = config["msa"]["database"]
        if not db or not Path(db).is_dir():
            errors.append(f"Missing MSA database directory: {db}")
        else:
            for key in ("db1", "db3"):
                file(Path(db) / (config["msa"][key] + ".dbtype"), key)
                resources["database:" + key] = {p.name: [p.stat().st_size, p.stat().st_mtime_ns]
                    for p in sorted(Path(db).glob(config["msa"][key] + "*")) if p.is_file()}
        if not config["msa"]["database_version"]:
            errors.append("Set msa.database_version to the installed release")
        if config["msa"].get("gpu"):
            # GPU-ready MMseqs databases carry extended dbtype flags; ordinary compressed CPU dbs do not.
            for key in ("db1", "db3"):
                path = Path(db or "") / (config["msa"][key] + ".dbtype")
                if path.is_file():
                    raw = path.read_bytes()
                    flags = (int.from_bytes(raw, "little") >> 16) & 0x7FFE
                    if len(raw) != 4 or not (flags & 8):
                        errors.append(f"GPU MSA requires prepared GPU database: {path}")
            if config["msa"]["threads"] > config["execution"]["resources"]["msa_gpu"]["cpus"]:
                errors.append("MSA threads exceed msa_gpu allocated CPUs")
    if stage == "fold":
        file(Path(config["folding"]["weights"]) / "protenix-v2.pt", "checkpoint")
        try:
            from configs.configs_model_type import model_configs
            if "protenix-v2" not in model_configs:
                errors.append("Installed Protenix does not support protenix-v2")
        except ImportError as exc:
            errors.append(f"Protenix configuration unavailable: {exc}")
        try:
            from configs.configs_data import data_configs
            for name in ("ccd_components_file", "ccd_components_rdkit_mol_file", "pdb_cluster_file", "obsolete_release_data_csv"):
                file(data_configs.get(name), name)
        except ImportError as exc:
            errors.append(f"Protenix data configuration unavailable: {exc}")
    return {"python": sys.version.split()[0], "executable": sys.executable, "versions": versions,
            "resources": resources, "code": code_signature(), "errors": errors}


def receipt_valid(directory, expected=None):
    path = Path(directory) / "complete.json"
    if not path.is_file():
        return False
    try:
        receipt = read_json(path)
        return bool(receipt["artifacts"]) and (expected is None or receipt["signature"] == expected) and all(
            Path(p).is_file() and Path(p).stat().st_size > 0 and digest(p) == h for p, h in receipt["artifacts"].items())
    except (OSError, ValueError, KeyError):
        return False


def execute(plan_path, task_id, memory_tier=0, attempt=1):
    if memory_tier != 0:
        raise ValueError("P2 requires explicit, unchanged inference parameters; memory-tier fallback is disabled")
    plan = read_json(plan_path)
    locks = Path(plan["config"]["output"]) / ".locks"
    locks.mkdir(exist_ok=True)
    with open(locks / task_id, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _execute(plan_path, task_id, memory_tier, attempt)


def _execute(plan_path, task_id, memory_tier=0, attempt=1):
    plan = read_json(plan_path)
    root = Path(plan["config"]["output"])
    task = plan["tasks"][task_id]
    directory = root / "tasks" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "complete.json").unlink(missing_ok=True)
    try:
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FOUNDRY_CHECKPOINT_DIRS"] = plan["config"]["foundry_weights"]
        actual = probe(task["stage"], plan["config"])
        if actual != plan["environment"][task["stage"]] or actual["errors"]:
            raise RuntimeError("Stage environment changed or dependencies are missing; run doctor and start a new output directory")
        if digest(plan["config"]["input"]) != plan["input_sha256"] or code_signature() != plan["code_signature"]:
            raise RuntimeError("Input or implementation changed since planning")
        if plan.get("msa_capsule") and read_json(plan["config"]["input"]) != plan["fold_records"]:
            raise RuntimeError("Transferred MSA records differ from frozen input")
        if plan.get("gpu_capsule"):
            from .gpu_transfer import identity
            if identity(plan["config"], task["stage"])["portable"] != plan["gpu_capsule"]["portable"]:
                raise RuntimeError("Transferred inference software/model/data identity changed")
            for relative, expected in plan["gpu_capsule"]["inputs"].items():
                path = (root / relative).resolve()
                if not path.is_relative_to(root.resolve()) or digest(path) != expected:
                    raise RuntimeError("Transferred inference input checksum mismatch")
        if plan.get("mode") != "folding" and digest(root / "reference.pdb") != plan["reference_sha256"]:
            raise RuntimeError("Normalized reference changed since planning")
        for dep in task["deps"]:
            if not receipt_valid(root / "tasks" / dep, plan["tasks"][dep]["signature"]):
                raise RuntimeError(f"Upstream artifact verification failed: {dep}")
        task["memory_tier"] = memory_tier
        runtime = {"attempt": attempt, "memory_tier": memory_tier, "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
                   "slurm_partition": os.environ.get("SLURM_JOB_PARTITION")}
        if task['stage'] in ('rfd3', 'mpnn', 'fold') and plan['config']['execution'].get('native_gpu'):
            import torch, torch_musa
            job_id = os.environ.get('ANTIGEN_NATIVE_JOB_ID')
            if not job_id or not torch.musa.is_available() or torch.musa.device_count() != 1:
                raise RuntimeError('Native GPU stage requires exactly one leased MUSA device and job identity')
            runtime.update(execution_backend='baremetal', job_id=job_id,
                           gpu=torch.musa.get_device_name(0), musa=torch_musa.__version__,
                           gpu_memory_bytes=torch.musa.get_device_properties(0).total_memory,
                           musa_visible_devices=os.environ.get('MUSA_VISIBLE_DEVICES'))
        elif task["stage"] in ("rfd3", "mpnn", "fold") and os.environ.get("SLURM_JOB_ID"):
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError("GPU stage has no CUDA device in its Slurm allocation")
            runtime.update(gpu=torch.cuda.get_device_name(0), cuda=torch.version.cuda,
                           gpu_memory_bytes=torch.cuda.get_device_properties(0).total_memory)
        elif task["stage"] == "msa" and plan["config"]["msa"].get("gpu") and os.environ.get("SLURM_JOB_ID"):
            import subprocess
            if not os.environ.get("CUDA_VISIBLE_DEVICES"):
                raise RuntimeError("GPU MSA requires Slurm CUDA_VISIBLE_DEVICES allocation")
            runtime["gpu"] = subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid", "--format=csv,noheader"], text=True).strip()
            runtime["cuda_visible_devices"] = os.environ["CUDA_VISIBLE_DEVICES"]
        if os.environ.get("ANTIGEN_MSA_POOL_CONTEXT"):
            runtime["msa_pool"] = {"context": os.environ["ANTIGEN_MSA_POOL_CONTEXT"],
                                   "request_id": os.environ.get("ANTIGEN_MSA_POOL_REQUEST")}
        runtime["effective_config"] = plan["config"]
        write_json(directory / "runtime.json", runtime)
        # Keep failed attempts, but use a fresh directory so stale outputs cannot pass validation.
        from . import adapters
        import resource, time
        started = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        getattr(adapters, task["stage"])(plan, task, directory)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        runtime["performance"] = {"stage_seconds": time.monotonic()-started,
            "child_user_seconds": after.ru_utime-before.ru_utime,
            "child_system_seconds": after.ru_stime-before.ru_stime,
            "max_child_rss_kib": after.ru_maxrss,
            "child_major_faults": after.ru_majflt-before.ru_majflt,
            "child_input_blocks": after.ru_inblock-before.ru_inblock,
            "note": "getrusage children; RSS is maximum child RSS, not concurrent aggregate"}
        write_json(directory / "runtime.json", runtime)
        artifacts = {str(p.resolve()): digest(p) for p in directory.rglob("*")
                     if p.is_file() and p.name not in ("complete.json", "error.json") and p.stat().st_size > 0}
        external = directory / "msa_artifacts.json"
        if external.is_file():
            artifacts.update(read_json(external))
        write_json(directory / "complete.json", {"signature": task["signature"], "artifacts": artifacts, "environment": actual})
    except Exception as exc:
        write_json(directory / "error.json", {"error": str(exc), "traceback": traceback.format_exc()})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    parser.add_argument("task")
    parser.add_argument("--memory-tier", type=int, choices=[0, 1, 2], default=0)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    execute(args.plan, args.task, args.memory_tier, args.attempt)


if __name__ == "__main__":
    main()
