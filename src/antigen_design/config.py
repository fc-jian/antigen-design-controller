from copy import deepcopy
from pathlib import Path
import random
import os
import yaml
from .structure import Protein, select_region, segments

DEFAULTS = {
    "mode": "design",
    "input": "", "output": "runs/design", "numbering": "parsed", "seed": 42,
    "schema_version": 1,
    "selection": {"regions": [], "region_sets": [], "surface": False, "threshold": 0.2},
    "rfd3": {"enabled": False, "samples": 2, "checkpoint": "rfd3", "options": {}},
    "mpnn": {"sequences": 2, "temperature": 0.1, "checkpoint": "proteinmpnn", "options": {}},
    "msa": {"method": "colabfold", "database": None, "database_version": None, "cache_root": None,
            "script": None, "mmseqs": "mmseqs", "batch_size": 1, "batch_scope": "all", "gpu": False,
            "gpu_server": False, "db_load_mode": 0, "ignore_index": False, "db1": "uniref30_2302_db",
            "db3": "colabfold_envdb_202108_db", "threads": 8,
            "optimization": {"enabled": False, "lanes": 1, "chunk_size": 32,
                             "staging": "none", "disk_root": "/tmp", "shm_root": "/dev/shm",
                             "reserve_gib": 16, "memory_budget_gib": 0, "working_memory_gib": 256}},
    "folding": {"model": "protenix-v2", "seeds": [101], "samples": 1, "need_atom_confidence": False,
                "weights": "", "data_root": None, "options": {}},
    "foundry_weights": "",
    "execution": {"backend": "local", "transport": "native", "ssh_skill": None, "remote_root": None, "source_dir": None,
        "gpu_routing": {"enabled": False, "poll_seconds": 45, "queue_grace_seconds": 60, "targets": []},
        "msa_remote": {"enabled": False, "host": "msa-server", "root": None, "python": None, "database": None, "script": None, "mmseqs": None, "cache_root": None, "source_dir": None, "transfer_command": [], "resources": {}, "fallback_when_busy": False, "poll_seconds": 30, "optimization": None, "pool": {"enabled": False, "idle_seconds": 300, "max_seconds": 7200, "request_timeout_seconds": 1800, "queue_limit": 32}},
        "max_gpu_jobs": 1, "max_msa_jobs": 1, "runtime_limits_file": None, "queue_notice_seconds": 7200, "h100_fallback": False,
        "cpu_host": "", "gpu_host": "", "msa_host": None, "ssh_binary": "ssh", "scp_binary": "scp",
        "shared_root": "/CONFIGURE/shared-root", "native_gpu": None,
        "python": {"prepare": "python", "rfd3": None, "mpnn": None, "msa": None, "fold": None, "metrics": "python"},
        "resources": {"cpu": {"cpus": 8, "memory": "16G", "time": "02:00:00"},
                      "msa_gpu": {"cpus": 8, "memory": "128G", "time": "02:00:00", "gres": "gpu:1"},
                      "metrics": {"cpus": 2, "memory": "4G", "time": "00:30:00"},
                      "gpu": {"cpus": 4, "memory": "32G", "time": "02:00:00", "gres": "gpu:1"}}},
}


def merge(base, patch, trail=""):
    result = deepcopy(base)
    for key, value in patch.items():
        if key not in base:
            raise ValueError(f"Unknown configuration key {trail}{key}")
        if isinstance(base[key], dict) and key != "options":
            if not isinstance(value, dict):
                raise ValueError(f"{trail}{key} must be an object")
            # Site-specific sbatch options are checked by the scheduler.
            if key in ("cpu", "gpu", "metrics", "msa_gpu") or (key == "resources" and trail == "execution.msa_remote."):
                result[key].update(value)
            else:
                result[key] = merge(base[key], value, f"{trail}{key}.")
        else:
            result[key] = value
    return result


def positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def load_config(path, server=None):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a YAML object")
    if server:
        site_path = Path(server).resolve()
        site = yaml.safe_load(site_path.read_text())
        if not isinstance(site, dict) or set(site) - {"execution", "msa", "folding", "foundry_weights"}:
            raise ValueError("Server profile permits execution, msa, folding, foundry_weights only")
        config = merge(merge(DEFAULTS, site), raw)
    else:
        config = merge(DEFAULTS, raw)
    if not config["input"]:
        raise ValueError("input must be supplied explicitly")
    for key in ("input", "output"):
        config[key] = str((path.parent / config[key]).resolve())
    for stage, executable in config["execution"]["python"].items():
        if executable and "/" in executable and not Path(executable).is_absolute():
            # Resolving the executable symlink would bypass its virtual environment.
            config["execution"]["python"][stage] = os.path.abspath(path.parent / executable)
    for key in ("database", "script"):
        value = config["msa"][key]
        if value:
            config["msa"][key] = str((path.parent / value).resolve())
    validate_config(config)
    return config


def validate_config(c):
    from .msa_remote import validate
    validate(c)
    from .gpu_routing import validate_routing
    validate_routing(c["execution"].get("gpu_routing", {}))
    if c["mode"] not in ("design", "folding"):
        raise ValueError("mode must be design or folding")
    if c["schema_version"] != 1:
        raise ValueError("Unsupported schema_version")
    if c["execution"]["h100_fallback"]:
        raise ValueError("P2 does not silently switch hardware/parameters; use explicit retry configuration")
    if c["numbering"] not in ("parsed", "original"):
        raise ValueError("numbering must be parsed or original")
    if c["folding"]["model"] != "protenix-v2":
        raise ValueError("Only protenix-v2 is supported; fallback is prohibited")
    if type(c["folding"]["need_atom_confidence"]) is not bool:
        raise ValueError("folding.need_atom_confidence must be boolean")
    if c["msa"]["method"] != "colabfold":
        raise ValueError("Only local ColabFold MSA is supported")
    if c["execution"]["backend"] not in ("local", "slurm"):
        raise ValueError("backend must be local or slurm")
    if c["execution"]["transport"] not in ("native", "ssh-skill", "openssh"):
        raise ValueError("transport must be native, openssh or ssh-skill")
    if c["execution"]["transport"] == "openssh":
        from pathlib import PurePosixPath
        from .transport import valid_host
        if not c["execution"]["remote_root"] or not PurePosixPath(c["execution"]["remote_root"]).is_absolute():
            raise ValueError("openssh requires an absolute execution.remote_root")
        for key in ("cpu_host", "gpu_host"):
            valid_host(c["execution"][key])
    if c["execution"]["transport"] == "ssh-skill":
        if not c["execution"]["ssh_skill"] or not c["execution"]["remote_root"]:
            raise ValueError("ssh-skill requires execution.ssh_skill and execution.remote_root")
        if not Path(c["execution"]["remote_root"]).is_absolute():
            raise ValueError("execution.remote_root must be an absolute remote directory")
    if type(c["execution"]["max_gpu_jobs"]) is not int or c["execution"]["max_gpu_jobs"] < 0:
        raise ValueError("execution.max_gpu_jobs must be an integer >= 0 (0 = unlimited)")
    policy = c["execution"].get("runtime_limits_file")
    if policy is not None and (not isinstance(policy, str) or not Path(policy).is_absolute()):
        raise ValueError("execution.runtime_limits_file must be an absolute local path")
    for key in ("max_msa_jobs", "queue_notice_seconds"):
        positive(c["execution"][key], "execution." + key)
    positive(c["msa"]["batch_size"], "msa.batch_size")
    if c["msa"]["batch_scope"] not in ("all", "backbone"):
        raise ValueError("msa.batch_scope must be all or backbone")
    opt = c["msa"]["optimization"]
    if type(opt["enabled"]) is not bool or opt["staging"] not in ("none", "disk", "hybrid", "indices"):
        raise ValueError("Invalid msa.optimization mode")
    for key in ("lanes", "chunk_size", "reserve_gib", "working_memory_gib"):
        positive(opt[key], "msa.optimization." + key)
    if type(opt["memory_budget_gib"]) is not int or opt["memory_budget_gib"] < 0:
        raise ValueError("msa.optimization.memory_budget_gib must be a nonnegative integer")
    if any(not Path(opt[key]).is_absolute() for key in ("disk_root", "shm_root")):
        raise ValueError("MSA staging roots must be absolute")
    if opt["enabled"] and c["msa"]["gpu"]:
        raise ValueError("CPU MSA optimization cannot be combined with GPU MSA")
    for key in ("gpu", "gpu_server", "ignore_index"):
        if type(c["msa"][key]) is not bool:
            raise ValueError(f"msa.{key} must be boolean")
    if c["msa"]["gpu_server"] and not c["msa"]["gpu"]:
        raise ValueError("msa.gpu_server requires msa.gpu")
    if type(c["msa"]["db_load_mode"]) is not int or c["msa"]["db_load_mode"] not in (0, 1, 2, 3):
        raise ValueError("msa.db_load_mode must be 0,1,2,3")
    if c["execution"].get("msa_host"):
        from .transport import valid_host
        valid_host(c["execution"]["msa_host"])
    region_sets = c["selection"]["region_sets"]
    if not isinstance(region_sets, list):
        raise ValueError("region_sets must be a list")
    if region_sets and (c["selection"]["regions"] or c["selection"]["surface"]):
        raise ValueError("region_sets cannot be combined with regions or surface")
    names = []
    for case in region_sets:
        if not isinstance(case, dict) or set(case) != {"name", "regions"} or not isinstance(case["name"], str) or not case["name"] or not isinstance(case["regions"], list):
            raise ValueError("Each region_set requires a name and regions list")
        names.append(case["name"])
    if len(names) != len(set(names)):
        raise ValueError("region_set names must be unique")
    for value, name in [(c["rfd3"]["samples"], "rfd3.samples"), (c["mpnn"]["sequences"], "mpnn.sequences"),
                        (c["folding"]["samples"], "folding.samples"), (c["msa"]["threads"], "msa.threads")]:
        positive(value, name)
    if type(c["seed"]) is not int or c["seed"] < 0:
        raise ValueError("seed must be a nonnegative integer")
    seeds = c["folding"]["seeds"]
    if not isinstance(seeds, list) or not seeds or any(type(s) is not int or s < 0 for s in seeds) or len(seeds) != len(set(seeds)):
        raise ValueError("folding.seeds must contain distinct nonnegative integers")
    if type(c["rfd3"]["enabled"]) is not bool or type(c["selection"]["surface"]) is not bool:
        raise ValueError("enabled/surface must be booleans")
    if not 0 <= c["selection"]["threshold"] <= 1:
        raise ValueError("Surface threshold must be between 0 and 1")
    if not 0 < c["mpnn"]["temperature"] <= 2:
        raise ValueError("MPNN temperature must be in (0,2]")
    if not isinstance(c["selection"]["regions"], list):
        raise ValueError("selection.regions must be a list")
    protected = {
        "rfd3": {"specification", "inputs", "out_dir", "diffusion_batch_size", "n_batches", "ckpt_path", "seed"},
        "mpnn": {"designed_residues", "fixed_residues", "designed_chains", "fixed_chains", "structure_path", "batch_size", "number_of_batches", "seed", "temperature", "structure_noise"},
    }
    for stage, keys in protected.items():
        if not isinstance(c[stage]["options"], dict) or keys.intersection(c[stage]["options"]):
            raise ValueError(f"{stage}.options cannot override pipeline constraints or sampling")
    if not isinstance(c["folding"]["options"], dict) or set(c["folding"]["options"]) - {"cycle", "step", "dtype", "trimul_kernel", "triatt_kernel", "chunk_size", "diffusion_chunk_size"}:
        raise ValueError("Unsupported folding options")


def selection(c, protein):
    settings = c["selection"]
    if settings["surface"] and settings["regions"]:
        raise ValueError("Use either surface or explicit regions; set regions: [] for surface")
    ranges, selected, sasa = [], set(), None
    if settings["surface"]:
        selected, sasa = protein.surface(settings["threshold"])
        ranges = [(part, [len(part), len(part)]) for part in segments(protein, selected)]
    else:
        for item in settings["regions"]:
            if isinstance(item, str):
                item = {"region": item}
            if not isinstance(item, dict) or set(item) - {"region", "length"}:
                raise ValueError(f"Invalid region configuration: {item}")
            indices = select_region(protein, item["region"], c["numbering"])
            if selected.intersection(indices):
                raise ValueError("Overlapping design regions are ambiguous")
            lengths = item.get("length", [len(indices), len(indices)])
            if not isinstance(lengths, list) or len(lengths) != 2 or any(type(n) is not int or n < 1 for n in lengths) or lengths[0] > lengths[1]:
                raise ValueError("length must be a positive closed interval [min,max]")
            if not c["rfd3"]["enabled"] and lengths != [len(indices), len(indices)]:
                raise ValueError("Variable-length regions require RFD3")
            selected.update(indices)
            ranges.append((indices, lengths))
    if not selected:
        raise ValueError("Design selection is empty")
    return sorted(ranges), selected, sasa


def build_plan(c):
    if c["mode"] == "folding":
        from .folding_input import build_fold_plan
        return build_fold_plan(c)
    if c["selection"]["region_sets"]:
        combined, chosen = [], set()
        first = None
        for case in c["selection"]["region_sets"]:
            subset = deepcopy(c)
            subset["selection"].update(region_sets=[], regions=case["regions"])
            subset["seed"] = c["seed"] + len(combined)
            partial = build_plan(subset)
            first = first or partial
            chosen.update(partial["selected"])
            for backbone in partial["backbones"]:
                backbone.update(id=f"b{len(combined):04d}", case=case["name"], selected=partial["selected"])
                combined.append(backbone)
        first.update(config=c, backbones=combined, selected=sorted(chosen),
                     expected_predictions=len(combined) * c["mpnn"]["sequences"] * len(c["folding"]["seeds"]) * c["folding"]["samples"])
        return first
    protein = Protein(c["input"])
    ranges, selected, sasa = selection(c, protein)
    rng = random.Random(c["seed"])
    backbones = []
    for sample in range(c["rfd3"]["samples"] if c["rfd3"]["enabled"] else 1):
        length_map = {indices[0]: (indices, rng.randint(*bounds)) for indices, bounds in ranges}
        mapping, contig, fixed = [], [], {}
        index, chain_pos, last_chain = 0, 0, None
        while index < len(protein.records):
            rec = protein.records[index]
            if rec.chain != last_chain:
                if last_chain is not None:
                    contig.append("/0")
                chain_pos, last_chain = 0, rec.chain
            if index in length_map:
                indices, length = length_map[index]
                contig.append(str(length))
                for offset in range(length):
                    chain_pos += 1
                    mapping.append({"chain": rec.normalized_chain, "position": chain_pos, "design": True,
                        "reference": indices[offset] if length == len(indices) else None,
                        "segment": indices, "length_changed": length != len(indices)})
                index = indices[-1] + 1
            else:
                contig.append(f"{rec.normalized_chain}{rec.position}")
                fixed[f"{rec.normalized_chain}{rec.position}"] = "ALL"
                chain_pos += 1
                mapping.append({"chain": rec.normalized_chain, "position": chain_pos,
                    "design": False, "reference": index, "length_changed": False})
                index += 1
        backbones.append({"id": f"b{sample:04d}", "seed": c["seed"] + sample,
            "mapping": mapping, "contig": ",".join(contig), "fixed_atoms": fixed})
    return {"schema_version": 1, "config": c, "reference_mapping": protein.mapping(), "selected": sorted(selected),
            "surface_rSASA": sasa, "surface_parameters": {"probe": 1.4, "points": 960, "normalization": "Wilke", "threshold": c["selection"]["threshold"]} if sasa else None,
            "warnings": protein.warnings, "backbones": backbones,
            "expected_predictions": len(backbones) * c["mpnn"]["sequences"] * len(c["folding"]["seeds"]) * c["folding"]["samples"]}
