"""Project-owned application storage and validated request compilation."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import tempfile
import threading
import uuid
from pathlib import Path
import yaml
from .config import DEFAULTS, merge, validate_config, build_plan
from .folding_input import integer, keys, parse_input
from .io import read_json, write_json, digest
from .structure import Protein


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Invalid application ID")
    return value


class Store:
    def __init__(self, repo, project="webapp"):
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project):
            raise ValueError("Invalid project directory name")
        self.submission_lock = threading.RLock()
        self.repo = Path(repo).resolve()
        self.root = self.repo / "projects" / project
        for name in ("inputs", "configs", "runs", "evidence", "docs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.registry = self.root / "runs" / "web-registry.json"
        if not self.registry.exists():
            write_json(self.registry, [])
        self.limits_path = self.root / "scheduler-limits.json"
        if not self.limits_path.exists():
            from .runtime_limits import DEFAULT_LIMITS
            write_json(self.limits_path, DEFAULT_LIMITS)

    def scheduler_limits(self, value=None):
        from .runtime_limits import validate
        if value is not None:
            write_json(self.limits_path, validate(value))
        return validate(read_json(self.limits_path))

    def archive(self, job, archived):
        import fcntl
        root = self.job_root(job)
        with open(root / ".runner.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Controller is still active") from None
            if read_json(root / "status.json")["state"] not in ("succeeded", "failed", "cancelled"):
                raise ValueError("Only stopped historical tasks can be archived")
            manifest = root / "pipeline/manifest.json"
            if manifest.exists() and any(s["state"] in ("submitting", "submitted", "running")
                                         for s in read_json(manifest)["tasks"].values()):
                raise ValueError("Active or ambiguous scheduler jobs prevent archiving")
            write_json(root / "archive.json", {"archived": archived, "updated_at": now()})
        return self.detail(job)

    def profiles(self):
        result = []
        for path in sorted((self.repo / "configs" / "servers").glob("*.yaml")):
            if "template" in path.stem:
                continue
            try:
                profile = yaml.safe_load(path.read_text())
                config = merge(DEFAULTS, profile)
                execution = config["execution"]
                if execution["backend"] != "slurm" or execution["transport"] != "openssh":
                    continue
                if any("CONFIGURE" in str(execution[k]) for k in ("remote_root", "cpu_host", "gpu_host")):
                    continue
                result.append({"id": path.stem, "cpu_host": execution["cpu_host"],
                               "gpu_host": execution["gpu_host"], "resources": execution["resources"],
                               "msa_backend": {"host": execution.get("msa_remote", {}).get("host") if execution.get("msa_remote", {}).get("enabled") else execution.get("msa_host") or execution["cpu_host"], "cross_storage": execution.get("msa_remote", {}).get("enabled", False), "fallback_when_busy": execution.get("msa_remote", {}).get("fallback_when_busy", False), "fallback_host": execution["cpu_host"], "pool": execution.get("msa_remote", {}).get("pool", {}), "resources": execution["msa_remote"]["resources"] if execution.get("msa_remote", {}).get("enabled") else execution["resources"]["cpu"]},
                               **self.scheduler_limits()})
                routing_path = self.repo / "configs" / "gpu-targets.yaml"
                if routing_path.exists():
                    result[-1]["gpu_routing"] = yaml.safe_load(routing_path.read_text())
            except (ValueError, KeyError, TypeError):
                continue
        return result

    def upload(self, filename, content):
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdb", ".cif", ".mmcif"):
            raise ValueError("Upload a PDB or mmCIF file")
        if not isinstance(content, str) or not content or len(content.encode()) > 20 * 1024 * 1024:
            raise ValueError("Structure must be between 1 byte and 20 MiB")
        asset = uuid.uuid4().hex
        directory = self.root / "inputs" / asset
        directory.mkdir()
        path = directory / ("structure" + suffix)
        path.write_text(content)
        protein = Protein(path)
        from Bio.PDB.Polypeptide import is_aa
        excluded = []
        for chain in protein.model:
            for residue in chain:
                if residue.id[0] != " ":
                    original_id = f"{chain.id}:{residue.id[1]}{residue.id[2].strip()}"
                    if is_aa(residue, standard=False):
                        raise ValueError(f"Unsupported modified protein residue {original_id}: {residue.resname}; no automatic repair")
                    excluded.append({"original_id": original_id, "resname": residue.resname})
        protein.normalized(directory / "viewer.pdb")
        from collections import Counter
        excluded_summary = "、".join(f"{name} × {count}" for name, count in Counter(r["resname"] for r in excluded).items())
        metadata = {"id": asset, "filename": Path(filename).name, "path": path.name,
                    "sha256": digest(path), "residues": protein.mapping(), "excluded_residues": excluded,
                    "warnings": protein.warnings + ([
                        f"未纳入设计上下文的非蛋白残基：{excluded_summary}。"] if excluded else [])}
        write_json(directory / "asset.json", metadata)
        return metadata

    def asset(self, asset):
        return read_json(self.root / "inputs" / identifier(asset) / "asset.json")

    def surface(self, asset):
        metadata = self.asset(asset)
        path = self.root / "inputs" / identifier(asset) / metadata["path"]
        if digest(path) != metadata["sha256"]:
            raise ValueError("Structure changed since upload; upload it again")
        protein = Protein(path)
        _, values = protein.surface(threshold=0.2, probe=1.4, points=960)
        # The UI excludes strictly < 0.2; Protein.surface's historical > rule
        # is intentionally not used to classify this interactive selection.
        return {"threshold": 0.2, "method": "Shrake–Rupley / Wilke (Tien 2013)",
                "probe_angstrom": 1.4, "points": 960, "context": "all_protein_chains",
                "residues": [{"index": r.index, "original_id": r.original_id,
                              "rsasa": value, "surface": value >= 0.2}
                             for r, value in zip(protein.records, values)]}

    def registered(self):
        jobs = read_json(self.registry)
        if not isinstance(jobs, list) or any(not isinstance(job, str) for job in jobs):
            raise ValueError("Invalid application registry; expected a list of job IDs")
        return jobs

    def job_root(self, job):
        identifier(job)
        if job not in self.registered():
            raise ValueError("Unknown application job")
        return self.root / "runs" / job

    def compile(self, request, input_path, output):
        keys(request, {"id", "name", "mode", "profile", "asset", "input", "regions", "rfd3",
                       "backbones", "sequences", "temperature", "seed", "seeds", "samples"}, "request")
        name = request.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError("任务名称需为 1–120 个字符")
        profiles = {p["id"] for p in self.profiles()}
        if request.get("profile") not in profiles:
            raise ValueError("请选择已配置的 Slurm 集群")
        config = self.profile_config(request["profile"])
        config.update(input=str(input_path), output=str(output), mode=request.get("mode"), numbering="original")
        if config["mode"] not in ("folding", "design"):
            raise ValueError("Invalid workflow")
        config["execution"].update(self.scheduler_limits())
        config["execution"]["runtime_limits_file"] = str(self.limits_path)
        config["folding"]["samples"] = integer(request.get("samples", config["folding"]["samples"]), 1, 20, "samples")
        seeds = request.get("seeds", config["folding"]["seeds"])
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= 16:
            raise ValueError("Provide 1–16 seeds")
        for seed in seeds:
            integer(seed, 0, 2**31 - 1, "seed")
        config["folding"]["seeds"] = seeds
        if config["mode"] == "design":
            config["seed"] = integer(request.get("seed", config["seed"]), 0, 2**31 - 1, "design seed")
            enabled = request.get("rfd3", config["rfd3"]["enabled"])
            if type(enabled) is not bool:
                raise ValueError("rfd3 must be boolean")
            config["rfd3"].update(enabled=enabled, samples=integer(request.get("backbones", config["rfd3"]["samples"]), 1, 50, "backbones"))
            config["mpnn"]["sequences"] = integer(request.get("sequences", config["mpnn"]["sequences"]), 1, 100, "sequences")
            temperature = request.get("temperature", config["mpnn"]["temperature"])
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 < temperature <= 2:
                raise ValueError("Temperature must be in (0, 2]")
            config["mpnn"]["temperature"] = temperature
            config["selection"]["regions"] = request.get("regions", [])
        validate_config(config)
        plan = build_plan(config)
        if plan["expected_predictions"] > 2000:
            raise ValueError("单次运行上限为 2,000 个预测，请拆分批次")
        return config, plan

    def profile_config(self, profile_id):
        config = merge(DEFAULTS, yaml.safe_load((self.repo / "configs/servers" / (profile_id + ".yaml")).read_text()))
        routing_path = self.repo / "configs/gpu-targets.yaml"
        if routing_path.exists():
            config["execution"]["gpu_routing"] = yaml.safe_load(routing_path.read_text())
        return config

    def input_text(self, request):
        text = request.get("input", "")
        if not isinstance(text, str) or len(text.encode()) > 4 * 1024 * 1024:
            raise ValueError("Input exceeds 4 MiB")
        parse_input(text, request.get("seeds", [101]))
        return text

    def design_path(self, request):
        asset = self.asset(request.get("asset"))
        path = self.root / "inputs" / asset["id"] / asset["path"]
        if digest(path) != asset["sha256"]:
            raise ValueError("Uploaded input changed")
        return path

    def preview(self, request):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            if request.get("mode") == "folding":
                path.write_text(self.input_text(request))
            else:
                path = self.design_path(request)
            config, plan = self.compile(request, path, Path(temp) / "output")
        result = {"expected_predictions": plan["expected_predictions"], "warnings": plan["warnings"],
                  "resources": config["execution"]["resources"], "model": config["folding"]["model"],
                  "stages": (["rfd3"] if config["rfd3"]["enabled"] else []) +
                            (["mpnn"] if config["mode"] == "design" else []) + ["msa", "fold", "metrics"],
                  "cpu_host": config["execution"]["cpu_host"], "gpu_host": config["execution"]["gpu_host"]}
        result["gpu_routing"] = config["execution"]["gpu_routing"]
        result["msa_fallback_when_busy"] = config["execution"].get("msa_remote", {}).get("fallback_when_busy",False)
        result["msa_fallback_host"] = config["execution"]["cpu_host"]
        result["msa_pool"] = config["execution"].get("msa_remote", {}).get("pool", {})
        result["msa_host"] = config["execution"]["msa_remote"]["host"] if config["execution"].get("msa_remote", {}).get("enabled") else config["execution"].get("msa_host") or config["execution"]["cpu_host"]
        if config["mode"] == "design":
            result["warnings"] = list(dict.fromkeys(result["warnings"] + self.asset(request["asset"])["warnings"]))
            result["selected"] = [plan["reference_mapping"][i]["original_id"] for i in plan["selected"]]
            result["backbone_lengths"] = [len(b["mapping"]) for b in plan["backbones"]]
        else:
            result["jobs"] = [r["job"] for r in plan["fold_records"]]
        return result

    def submit(self, request):
        job = identifier(request.get("id"))
        if job in self.registered():
            original = read_json(self.job_root(job) / "request.json")
            if original != request:
                raise ValueError("Request ID already belongs to a different submission")
            return self.detail(job)
        directory = self.root / "runs" / job
        # Parse and compile privately, outside the short registry publication lock.
        with tempfile.TemporaryDirectory() as temp:
            if request.get("mode") == "folding":
                text = self.input_text(request)
                path = Path(temp) / "input.json"
                path.write_text(text)
            else:
                path = self.design_path(request)
            config, plan = self.compile(request, path, directory / "pipeline")
        with self.submission_lock:
            if job in self.registered():
                if read_json(self.job_root(job) / "request.json") != request:
                    raise ValueError("Request ID already belongs to a different submission")
                return self.detail(job)
            directory.mkdir(exist_ok=True)
            if request["mode"] == "folding":
                path = self.root / "inputs" / (job + ".json")
                path.write_text(text)
            config["input"] = str(path)
            write_json(self.root / "configs" / (job + ".json"), config)
            write_json(directory / "request.json", request)
            write_json(directory / "status.json", {"state": "queued", "created_at": now(), "updated_at": now(),
                                                   "expected_predictions": plan["expected_predictions"]})
            write_json(self.registry, [job, *self.registered()])
        return self.detail(job)

    def detail(self, job):
        self.job_root(job)
        try:
            return self._detail(job)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self.unavailable(job, exc)

    def result_view(self, job, index):
        from .folding_results import annotate_design
        root = self.job_root(job)
        results = read_json(root / "results.json")
        if index < 0 or index >= len(results):
            raise ValueError("Unknown result")
        result = read_json(root / f"result-{index}.json")
        return annotate_design(result, read_json(root / "pipeline/plan.json"))

    def unavailable(self, job, exc):
        return {"id": job, "name": job, "mode": "unknown", "profile": "", "state": "attention",
                "archived": False, "tasks": [], "results": [], "unavailable": True,
                "error": f"Run record unavailable: {exc}", "task_count": 0, "completed_tasks": 0}

    def list_jobs(self, archive="current", profile="", state="", offset=0, limit=25):
        active = {"queued", "starting", "running", "collecting", "cancelling"}
        if archive not in {"current", "archived", "all"} or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid job filters or pagination")
        items = []
        counts = {"total": 0, "active": 0, "succeeded": 0, "problem": 0}
        for job in self.registered():
            try:
                root = self.job_root(job)
                req, status = read_json(root / "request.json"), read_json(root / "status.json")
                item = {"id": job, **{k: req[k] for k in ("name", "mode", "profile")},
                        "state": status["state"],
                        **{k: status[k] for k in ("created_at", "updated_at") if k in status},
                        "archived": read_json(root / "archive.json")["archived"] if (root / "archive.json").exists() else False}
                if not all(isinstance(item[k], str) for k in ("name", "mode", "profile", "state")):
                    raise ValueError("Invalid run metadata")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                item = self.unavailable(job, exc)
            counts["total"] += 1
            counts["active"] += item["state"] in active
            counts["succeeded"] += item["state"] == "succeeded"
            counts["problem"] += item["state"] in {"attention", "failed"}
            if archive != "all" and item["archived"] != (archive == "archived"):
                continue
            if profile and item["profile"] != profile:
                continue
            matches = item["state"] in active if state == "active" else item["state"] in {"attention", "failed"} if state == "problem" else item["state"] == state
            if state and not matches:
                continue
            items.append(item)
        page = items[offset:offset + limit]
        for item in page:
            try:
                path = self.job_root(item["id"]) / "pipeline/manifest.json"
                tasks = read_json(path)["tasks"].values() if path.exists() else []
                item.update(task_count=len(tasks), completed_tasks=sum(t["state"] == "succeeded" for t in tasks))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                item.update(task_count=0, completed_tasks=0, error=f"Run record unavailable: {exc}", unavailable=True)
        return {"items": page, "total": len(items), "offset": offset, "limit": limit, "counts": counts}

    def _detail(self, job):
        root = self.job_root(job)
        req, status = read_json(root / "request.json"), read_json(root / "status.json")
        result = {"id": job, "name": req["name"], "mode": req["mode"], "profile": req["profile"], **status,
                  "tasks": [], "results": []}
        result["archived"] = read_json(root / "archive.json")["archived"] if (root / "archive.json").exists() else False
        if (root / "pipeline" / "plan.json").exists() and not (root / "pipeline" / "manifest.json").exists():
            result["initializing"] = True
            result["error"] = "Initialization is incomplete; waiting for manifest. If the controller stopped, inspect its log; do not recreate run records."
        if (root / "pipeline" / "plan.json").exists() and (root / "pipeline" / "manifest.json").exists():
            plan = read_json(root / "pipeline" / "plan.json")
            manifest = read_json(root / "pipeline" / "manifest.json")
            monitor = read_json(root / "monitor.json") if (root / "monitor.json").exists() else {}
            for name, task in plan["tasks"].items():
                result["tasks"].append({"id": name, "stage": task["stage"], "host": task["host"],
                                        **manifest["tasks"][name], "observation": monitor.get("tasks", {}).get(name)})
                item = result["tasks"][-1]
                if item.get("route"):
                    item["route"] = {k:v for k,v in item["route"].items()
                                     if k in ("id", "target", "resources", "input_sha256", "input_bytes")}
                item["history"] = [{k:v for k,v in h.items() if k in
                    ("host", "job_id", "attempts", "slurm_state", "state", "error", "resources")}
                    for h in item.get("history", [])]
            result["observed_at"] = monitor.get("observed_at")
            result["monitor_error"] = monitor.get("error")
            result["gpu_routing"] = manifest.get("gpu_routing")
        if (root / "results.json").exists():
            result["results"] = read_json(root / "results.json")
        log = root / "controller.log"
        if log.exists():
            with open(log, "rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16000))
                result["controller_log"] = stream.read().decode(errors="replace")
        return result
