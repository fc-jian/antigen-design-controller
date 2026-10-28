"""Verified, explicit inference capsules across non-shared filesystems.

Original plans/receipts remain immutable. A returned capsule is imported with a
separate receipt recording both identities, execution site and transfer hashes.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import shutil
import tarfile
from .io import digest, fingerprint, read_json, write_json


def extract(archive, destination, expected):
    if digest(archive) != expected:
        raise ValueError("Transfer archive checksum mismatch")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        if len({m.name for m in members}) != len(members):
            raise ValueError("Duplicate transfer members")
        if sum(m.size for m in members) > 8 * 1024**3:
            raise ValueError("Inference capsule exceeds 8 GiB")
        for m in members:
            if not m.isfile() or not (destination / m.name).resolve().is_relative_to(destination):
                raise ValueError("Unsafe transfer member")
        for m in members:
            path = destination / m.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(m) as source, open(path, "wb") as target:
                shutil.copyfileobj(source, target)


def archive_files(directory, target):
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    return {"path": str(target), "sha256": digest(target), "bytes": Path(target).stat().st_size}


def identity(config, stage="fold"):
    """Portable identity: exact software sources, versions and model/data bytes."""
    from .worker import probe
    import importlib.util
    report = probe(stage, config)
    if report["errors"]:
        raise ValueError("; ".join(report["errors"]))
    resources = {}
    for name, value in report["resources"].items():
        resources[name] = digest(value["path"]) if isinstance(value, dict) and "path" in value else value
    for module in (("configs",) if stage == "fold" else ("foundry", "atomworks")):
        spec = importlib.util.find_spec(module)
        if spec and spec.submodule_search_locations:
            resources["source:" + module] = fingerprint({
                str(p.relative_to(base)): digest(p)
                for location in spec.submodule_search_locations for base in [Path(location)]
                for p in sorted(base.rglob("*.py"))})
    return {"environment": report, "portable": {"python": report["python"],
            "versions": report["versions"], "resources": resources}}


def pack(plan, request):
    from .adapters import sequence_record
    from .worker import receipt_valid
    name, route = request["task"], request["route"]
    original = plan["tasks"][name]
    stage = original["stage"]
    if stage not in ("fold", "rfd3", "mpnn"):
        raise ValueError("Unsupported GPU capsule stage")
    root = Path(plan["config"]["output"])
    for dep in original["deps"]:
        if not receipt_valid(root / "tasks" / dep, plan["tasks"][dep]["signature"]):
            raise ValueError("Upstream receipt failed before transfer")
    record = sequence_record(plan, original) if stage == "fold" else None
    directory = root / "transfers" / route["id"] / "input"
    if directory.exists():
        raise ValueError("Capsule input already exists; use its saved transfer receipt")
    directory.mkdir(parents=True)
    capsule = deepcopy(plan)
    c = capsule["config"]
    target_root = Path(route["root"])
    c["output"] = str(target_root)
    input_name = "input" + Path(c["input"]).suffix
    shutil.copyfile(c["input"], directory / input_name)
    c["input"] = str(target_root / input_name)
    if plan.get("mode") != "folding":
        shutil.copyfile(root / "reference.pdb", directory / "reference.pdb")
    if stage == "fold":
        from .gpu_routing import folding_options
        c["folding"]["weights"] = route["target"]["weights"]
        c["folding"]["data_root"] = route["target"]["data_root"]
        c["folding"]["options"] = folding_options(c, route["target"])
    else:
        c["foundry_weights"] = route["target"]["design"]["weights"]
    c["execution"].update(remote_root=route["target"]["root"],
        source_dir=route["source_dir"], gpu_host=route["target"]["host"])
    python = route["target"]["python"] if stage == "fold" else route["target"]["design"]["python"]
    c["execution"]["python"][stage] = python
    c["execution"]["python"]["metrics"] = python
    c["execution"]["resources"]["gpu"] = route["resources"]
    if route['target'].get('backend') == 'baremetal':
        c['execution']['native_gpu'] = route['target']['native']
    c["execution"]["gpu_routing"]["enabled"] = False
    transferred = {}
    if stage == "mpnn" and original["deps"]:
        # Preserve the conventional backbone path without inventing a dependency receipt.
        from .adapters import backbone_path
        source = backbone_path(plan, original)
        artifacts = read_json(root / "tasks" / original["deps"][0] / "complete.json")["artifacts"]
        if artifacts.get(str(source)) != digest(source):
            raise ValueError("Design backbone is not covered by the upstream receipt")
        relative = source.relative_to(root)
        (directory / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, directory / relative)
        transferred[str(source)] = {"path": str(relative), "sha256": digest(source)}
    jobs = []
    if stage == "fold":
        msa = original["deps"][0]
        filename = Path("fold_inputs") / (record["id"] + ".json") if "members" in plan["tasks"][msa] else Path("fold_input.json")
        jobs = read_json(root / "tasks" / msa / filename)
        original_artifacts = read_json(root / "tasks" / msa / "complete.json")["artifacts"]
    for job in jobs:
        for entity in job["sequences"]:
            protein = entity.get("proteinChain", {})
            for key in ("pairedMsaPath", "unpairedMsaPath"):
                if not protein.get(key):
                    continue
                path = Path(protein[key])
                checksum = digest(path)
                if original_artifacts.get(str(path)) != checksum:
                    raise ValueError("MSA file is not covered by the validated upstream receipt")
                relative = "msa/" + checksum + ".a3m"
                (directory / "msa").mkdir(exist_ok=True)
                shutil.copyfile(path, directory / relative)
                transferred[str(path)] = {"path": relative, "sha256": checksum}
                protein[key] = str(target_root / relative)
    if stage == "fold":
        write_json(directory / "fold_input.json", jobs)
    write_json(directory / "origin-plan.json", plan)
    for dep in original["deps"]:
        write_json(directory / f"origin-{dep}-receipt.json", read_json(root / "tasks" / dep / "complete.json"))
    inputs = {str(p.relative_to(directory)): digest(p) for p in directory.rglob("*") if p.is_file()}
    capsule["gpu_capsule"] = {"stage": stage, "record": record, "inputs": inputs, "transferred": transferred,
        "origin_identity": plan["identity"], "origin_task_signature": original["signature"],
        "route_id": route["id"], "portable": route["portable"]}
    capsule["environment"] = {stage: route["environment"]}
    capsule["identity"] = fingerprint({"origin": plan["identity"], "route": route, "inputs": inputs})
    task = deepcopy(original)
    task.update(deps=[], host=route["target"]["host"])
    task["signature"] = fingerprint({"origin_task": original, "capsule": capsule["identity"]})
    capsule["tasks"] = {name: task}
    write_json(directory / "plan.json", capsule)
    result = archive_files(directory, directory.parent / "input.tar.gz")
    result["plan"] = capsule
    write_json(directory.parent / "input-transfer.json", result)
    return result


def results(plan, request):
    from .worker import receipt_valid
    name = request["task"]
    root = Path(plan["config"]["output"])
    if not receipt_valid(root / "tasks" / name, plan["tasks"][name]["signature"]):
        raise ValueError("Cannot transfer incomplete inference artifacts")
    directory = root / f"export-a{request['attempt']}"
    directory.mkdir(exist_ok=True)
    source = root / "tasks" / name
    receipt = read_json(source / "complete.json")
    for path in [*receipt["artifacts"], str(source / "complete.json")]:
        relative = Path(path).relative_to(source)
        target = directory / "task" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(root / "plan.json", directory / "capsule-plan.json")
    log = root / "logs" / f"{name}_a{request['attempt']}.log"
    if log.is_file():
        shutil.copyfile(log, directory / "slurm.log")
    return archive_files(directory, root / "result.tar.gz")


def import_results(plan, request):
    """Import only the capsule bound to this original task, preserving evidence."""
    from .worker import receipt_valid
    root = Path(plan["config"]["output"])
    name, route = request["task"], request["route"]
    destination = root / "tasks" / name
    if destination.exists():
        receipt = read_json(destination / "complete.json")
        if receipt.get("route_id") == route["id"] and receipt_valid(destination, plan["tasks"][name]["signature"]):
            return {"valid": True}
        raise ValueError("Refusing to replace a different inference result")
    work = root / "transfers" / route["id"] / "returned"
    extract(request["path"], work, request["sha256"])
    capsule = read_json(work / "capsule-plan.json")
    if capsule != route["plan"]:
        raise ValueError("Returned capsule plan differs from submitted plan")
    proof = capsule["gpu_capsule"]
    if (proof["origin_identity"] != plan["identity"]
            or proof["origin_task_signature"] != plan["tasks"][name]["signature"]
            or proof["route_id"] != route["id"]):
        raise ValueError("Returned capsule lineage mismatch")
    receipt = read_json(work / "task" / "complete.json")
    if (receipt["signature"] != capsule["tasks"][name]["signature"] or not receipt["artifacts"]
            or receipt["environment"] != capsule["environment"][plan["tasks"][name]["stage"]]):
        raise ValueError("Invalid capsule worker receipt")
    remote_task = Path(capsule["config"]["output"]) / "tasks" / name
    covered = set()
    for path, expected in receipt["artifacts"].items():
        relative = Path(path).relative_to(remote_task)
        local = work / "task" / relative
        if not local.resolve().is_relative_to((work / "task").resolve()) or digest(local) != expected:
            raise ValueError("Returned inference artifact checksum mismatch")
        covered.add(relative.as_posix())
    actual_files = {p.relative_to(work / "task").as_posix() for p in (work / "task").rglob("*")
                    if p.is_file() and p.name != "complete.json"}
    if actual_files != covered:
        raise ValueError("Unreceipted files in returned inference task")
    stage = plan["tasks"][name]["stage"]
    required_files = {"fold": ("predictions.json", "command.json"),
                      "rfd3": ("backbone.pdb", "generated.cif", "mapping.json", "rfd3_engine.json"),
                      "mpnn": ("sequences.json", "mpnn_input.json")}[stage]
    for required in ("runtime.json", *required_files):
        if required not in covered:
            raise ValueError("Incomplete capsule result")
    runtime = read_json(work / 'task' / 'runtime.json')
    if str(runtime.get('job_id') or runtime.get("slurm_job_id")) != str(request["job_id"]):
        raise ValueError("Capsule runtime belongs to a different Slurm job")
    if route['target'].get('backend') == 'baremetal' and (
            runtime.get('execution_backend') != 'baremetal' or runtime.get('slurm_job_id') is not None):
        raise ValueError('Native capsule runtime has incorrect execution provenance')
    predictions = read_json(work / "task" / "predictions.json") if stage == "fold" else []
    if stage == "fold" and len(predictions) != plan["config"]["folding"]["samples"]:
        raise ValueError("Returned prediction count differs from frozen plan")
    for prediction in predictions:
        for key in ("path", "confidence"):
            relative = Path(prediction[key]).relative_to(remote_task)
            if relative.as_posix() not in covered:
                raise ValueError("Prediction not covered by capsule receipt")
            prediction[key] = str(destination / relative)
    if stage in ("rfd3", "mpnn"):
        validate_design_result(plan, name, work / "task")
    (work / "task" / "complete.json").rename(work / "task" / "capsule-receipt.json")
    if stage == "fold":
        shutil.copyfile(work / "task" / "predictions.json", work / "task" / "capsule-predictions.json")
    shutil.copyfile(work / "capsule-plan.json", work / "task" / "capsule-plan.json")
    if (work / "slurm.log").exists():
        shutil.copyfile(work / "slurm.log", work / "task" / "capsule-slurm.log")
    if stage == "fold":
        write_json(work / "task" / "predictions.json", predictions)
    write_json(work / "task" / "transfer.json", {
        "route_id": route["id"], "host": route["target"]["host"], "job_id": request["job_id"],
        "archive_sha256": request["sha256"], "origin_identity": plan["identity"],
        "capsule_identity": capsule["identity"], "portable_identity": route["portable"]})
    # Publish directory+receipt atomically so a controller crash is recoverable.
    destination.parent.mkdir(exist_ok=True)
    artifacts = {str(destination / p.relative_to(work / "task")): digest(p)
                 for p in (work / "task").rglob("*") if p.is_file()}
    write_json(work / "task" / "complete.json", {"kind": "verified_gpu_capsule_import",
        "signature": plan["tasks"][name]["signature"], "route_id": route["id"],
        "artifacts": artifacts, "environment": receipt["environment"]})
    os.rename(work / "task", destination)
    return {"valid": True}


def validate_design_result(plan, name, directory):
    """Validate returned design geometry, mapping and original candidate provenance."""
    import numpy as np
    from .structure import Protein
    from .adapters import require_order, validate_sequence, backbone_path
    from .metrics import validate_fixed
    from .contracts import candidate_record
    task = plan["tasks"][name]
    backbone = plan["backbones"][task["backbone"]]
    reference = Protein(plan["config"]["input"])
    if task["stage"] == "rfd3":
        if read_json(directory / "mapping.json") != backbone["mapping"]:
            raise ValueError("Returned backbone mapping differs from frozen plan")
        for filename in ("generated.cif", "backbone.pdb"):
            protein = Protein(directory / filename)
            require_order(protein, backbone["mapping"])
            validate_fixed(reference, protein, backbone["mapping"])
    else:
        records = read_json(directory / "sequences.json")
        if len(records) != plan["config"]["mpnn"]["sequences"]:
            raise ValueError("Returned MPNN sample count differs from frozen plan")
        original = Protein(backbone_path(plan, task))
        atoms = lambda p: np.array([r[a].coord for r in p.residues for a in ("N", "CA", "C", "O")])
        for index, record in enumerate(records):
            protein = Protein(directory / f"s{index:04d}.cif")
            require_order(protein, backbone["mapping"])
            sequence = "".join(r.amino_acid for r in protein.records)
            validate_sequence(sequence, backbone["mapping"], reference)
            chains = {}
            for aa, item in zip(sequence, backbone["mapping"]):
                chains[item["chain"]] = chains.get(item["chain"], "") + aa
            if record != candidate_record(plan, backbone["id"], index, chains, backbone["seed"]):
                raise ValueError("Returned MPNN candidate provenance mismatch")
            if not np.allclose(atoms(protein), atoms(original), atol=0.005, rtol=0):
                raise ValueError("Returned MPNN changed backbone coordinates")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["identity", "extract"])
    parser.add_argument("request")
    args = parser.parse_args()
    data = json.loads(args.request)
    result = identity(data.get("config", data), data.get("stage", "fold")) if args.operation == "identity" else extract(data["path"], data["root"], data["sha256"])
    print(json.dumps(result))


if __name__ == "__main__":
    main()
