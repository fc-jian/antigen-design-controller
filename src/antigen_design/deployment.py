"""Portable, file-based configuration for the local web controller."""
from copy import deepcopy
import os
from pathlib import Path
import re
import yaml
from .config import DEFAULTS, merge


WEB_DEFAULTS = {
    "version": 1, "host": "127.0.0.1", "port": 8776, "repo": "..",
    "project": "webapp", "read_only": False,
    "server_profile": None, "gpu_targets": None,
    "resource_policy": "web-resources.local.json",
    "resource_limits": "web-resource-limits.local.json",
    "max_gpu_jobs": 1, "max_msa_jobs": 1, "queue_grace_seconds": 120,
    "scientific_defaults": {
        "msa": {"batch_size": 16, "batch_scope": "backbone"},
        "folding": {"need_atom_confidence": True},
    },
}


def settings(repo, path=None):
    repo = Path(repo).resolve()
    explicit = path or os.environ.get("ANTIGEN_WEB_CONFIG")
    path = Path(explicit).expanduser().resolve() if explicit else repo / "configs/webapp.yaml"
    if explicit and not path.is_file():
        raise ValueError(f"Web configuration not found: {path}")
    raw = yaml.safe_load(path.read_text()) if path.is_file() else {}
    if not isinstance(raw, dict) or set(raw) - WEB_DEFAULTS.keys():
        raise ValueError("Unknown web configuration fields or invalid YAML object")
    value = deepcopy(WEB_DEFAULTS)
    value.update(raw)
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported web configuration version")
    if value["host"] != "127.0.0.1":
        raise ValueError("The single-user web server must bind to 127.0.0.1; use an SSH tunnel for access")
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    if type(value["read_only"]) is not bool:
        raise ValueError("read_only must be boolean")
    if not isinstance(value["project"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value["project"]):
        raise ValueError("project must be one directory name containing letters, digits, hyphens or underscores")
    from .runtime_limits import validate
    validate({key: value[key] for key in ("max_gpu_jobs", "max_msa_jobs")})
    if type(value["queue_grace_seconds"]) is not int or not 30 <= value["queue_grace_seconds"] <= 86400:
        raise ValueError("queue_grace_seconds must be 30–86400")
    scientific = value["scientific_defaults"]
    if not isinstance(scientific, dict) or set(scientific) - {"seed", "selection", "rfd3", "mpnn", "msa", "folding"}:
        raise ValueError("scientific_defaults accepts scientific settings only")
    defaults = deepcopy(WEB_DEFAULTS['scientific_defaults'])
    for key, item in scientific.items():
        defaults[key] = (defaults.get(key, {}) | item) if isinstance(item, dict) else item
    value["scientific_defaults"] = defaults
    from .config import validate_config
    validate_config(merge(DEFAULTS, value["scientific_defaults"]))
    for key in ("repo", "server_profile", "gpu_targets", "resource_policy", "resource_limits"):
        if value[key] is None and key in ("server_profile", "gpu_targets"):
            continue
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"{key} must be a path")
        value[key] = str((path.parent / Path(value[key]).expanduser()).resolve())
    if not path.is_file():
        value["repo"] = str(repo)
    value["config_path"] = str(path)
    return value


def profile(repo):
    value = settings(repo)
    path = value["server_profile"]
    if path is None:
        return None
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or set(raw) - {"execution", "msa", "folding", "foundry_weights"}:
        raise ValueError("Server profile accepts execution, msa, folding and foundry_weights only")
    config = merge(merge(DEFAULTS, raw), value["scientific_defaults"])
    if config["execution"]["backend"] != "slurm" or config["execution"]["transport"] != "openssh":
        raise ValueError("The web controller requires an OpenSSH Slurm profile; use the CLI for local execution")
    from .config import validate_config
    if value["gpu_targets"]:
        config["execution"]["gpu_routing"] = yaml.safe_load(Path(value["gpu_targets"]).read_text())
    validate_config(config)
    return config
