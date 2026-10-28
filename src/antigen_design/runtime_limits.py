"""Mutable admission policy, separate from immutable scientific run identity."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import re
from .io import read_json

DEFAULT_LIMITS = {"max_gpu_jobs": 0, "max_msa_jobs": 8}
ACTIVE = {"submitting", "submitted", "running"}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_LIMITS):
        raise ValueError("Provide max_gpu_jobs and max_msa_jobs")
    for key, minimum in (("max_gpu_jobs", 0), ("max_msa_jobs", 1)):
        if type(value[key]) is not int or value[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    return dict(value)


@contextmanager
def admission(plan):
    execution = plan["config"]["execution"]
    path = execution.get("runtime_limits_file")
    if not path:
        yield execution, []
        return
    path = Path(path)
    # Serialize admission across local web controllers, not compute execution.
    # This closes the read-count/submit race between different application runs.
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        limits = validate(read_json(path))
        others = []
        own = Path(plan["local_output"]).resolve()
        for job in read_json(path.parent / "runs/web-registry.json"):
            if not re.fullmatch(r"[a-f0-9]{32}", job):
                raise ValueError("Invalid registered application job")
            root = path.parent / "runs" / job / "pipeline"
            manifest = root / "manifest.json"
            if root.resolve() != own and manifest.exists():
                others.extend(s for s in read_json(manifest)["tasks"].values() if s["state"] in ACTIVE)
        yield limits, others
