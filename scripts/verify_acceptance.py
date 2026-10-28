"""Validate real downloaded evidence without extracting arbitrary archive paths.

External MSA cache files are checked on the server by the controller receipts, not
claimed as independently hashed locally. No sequences are printed in the report.
"""
import argparse
import csv
import json
from pathlib import Path
import tarfile
import tempfile
import numpy as np
from antigen_design.io import digest, read_json, write_json
from antigen_design.structure import Protein


def reference_path(plan, override=None):
    """Allow explicit relocation without rewriting the historical plan."""
    path = Path(override if override is not None else plan["local_input"])
    if digest(path) != plan["input_sha256"]:
        raise ValueError("Reference input checksum differs from historical plan")
    return path


def verify(root, reference=None, output=None):
    root = Path(root).resolve()
    plan = read_json(root / "plan.json")
    manifest = read_json(root / "manifest.json")
    if plan["dry_run"] or not all(t["state"] == "succeeded" for t in manifest["tasks"].values()):
        raise ValueError("Real acceptance requires every stage to have succeeded")
    bundle = root / "deliverables.tar.gz"
    expected = read_json(root / "download_verification.json")
    if digest(bundle) != expected["sha256"]:
        raise ValueError("Downloaded archive checksum mismatch")
    remote_root = Path(plan["config"]["output"])
    rows = list(csv.DictReader(open(root / "summary.csv")))
    if len(rows) != plan["expected_predictions"] or any(r["status"] != "succeeded" for r in rows):
        raise ValueError("Prediction accounting mismatch")
    import hashlib
    internal, external = 0, 0
    runtime_records, independent, fixed_checks = {}, [], []
    reference = Protein(reference_path(plan, reference))
    with tarfile.open(bundle, "r:gz") as archive:
        def data(name):
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError("Expected a regular archive member")
            return archive.extractfile(member).read()
        def record(name):
            return json.loads(data(name))
        if hashlib.sha256(data(Path(plan["config"]["input"]).name)).hexdigest() != plan["input_sha256"]:
            raise ValueError("Archived input mismatch")
        for name, task in plan["tasks"].items():
            receipt = record(f"tasks/{name}/complete.json")
            if receipt["signature"] != task["signature"] or receipt["environment"] != plan["environment"][task["stage"]]:
                raise ValueError("Task receipt identity mismatch")
            for path, wanted in receipt["artifacts"].items():
                p = Path(path)
                if not p.is_relative_to(remote_root):
                    external += 1
                    continue
                raw = data(p.relative_to(remote_root).as_posix())
                if hashlib.sha256(raw).hexdigest() != wanted:
                    raise ValueError(f"Corrupt artifact: {path}")
                internal += 1
            runtime = record(f"tasks/{name}/runtime.json")
            if str(runtime["slurm_job_id"]) != manifest["tasks"][name]["job_id"]:
                raise ValueError("Runtime job ID differs from manifest")
            if task["stage"] in ("rfd3", "mpnn", "fold") and not runtime.get("gpu"):
                raise ValueError("GPU stage lacks actual device evidence")
            runtime_records[name] = {k: runtime.get(k) for k in ("slurm_job_id", "slurm_partition", "gpu", "cuda", "attempt")}
            if task["stage"] == "rfd3":
                mapping = plan["backbones"][task["backbone"]]["mapping"]
                with tempfile.TemporaryDirectory() as tmp:
                    local = Path(tmp) / "backbone.pdb"
                    local.write_bytes(data(f"tasks/{name}/backbone.pdb"))
                    generated = Protein(local)
                    from antigen_design.metrics import validate_fixed
                    validate_fixed(reference, generated, mapping)
                    fixed_checks.append({"task": name, "retained_residues": sum(not m["design"] for m in mapping),
                                         "retained_backbone_tolerance_angstrom": 0.05,
                                         "sequence_and_backbone_constraints": "passed"})
            if task["stage"] == "mpnn":
                candidates = record(f"tasks/{name}/sequences.json")
                if len(candidates) != plan["config"]["mpnn"]["sequences"]:
                    raise ValueError("Candidate accounting mismatch")
                mapping = plan["backbones"][task["backbone"]]["mapping"]
                for candidate in candidates:
                    sequence = candidate["sequence"]
                    if len(sequence) != len(mapping):
                        raise ValueError("Candidate length mismatch")
                    for aa, item in zip(sequence, mapping):
                        if not item["design"] and aa != reference.records[item["reference"]].amino_acid:
                            raise ValueError("Retained sequence was changed")
        for row in rows:
            path = Path(row["prediction"])
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / path.name
                local.write_bytes(data(path.relative_to(remote_root).as_posix()))
                prediction = Protein(local)
                mapping = next(b["mapping"] for b in plan["backbones"] if b["id"] == row["backbone"])
                mapped = [m for m in mapping if m["reference"] is not None]
                expected_ids = [(m["chain"], m["position"]) for m in mapped]
                lookup = {(r.chain, r.position): xyz for r, xyz in zip(prediction.records, prediction.ca())}
                xyz = np.array([lookup[key] for key in expected_ids])
                # Independent Kabsch formula, not metrics.fit().
                reference_xyz = reference.ca()[[m["reference"] for m in mapped]]
                x, y = xyz - xyz.mean(0), reference_xyz - reference_xyz.mean(0)
                u, _, vt = np.linalg.svd(x.T @ y)
                rotation = u @ np.diag([1, 1, np.linalg.det(u @ vt)]) @ vt
                rmsd = float(np.sqrt(np.mean(np.sum((x @ rotation - y) ** 2, axis=1))))
                if not np.isclose(rmsd, float(row["reference_all_rmsd_fit_all"]), atol=1e-5):
                    raise ValueError("Independent RMSD differs from reported value")
                independent.append({"prediction_id": row["prediction_id"], "independent_ca_rmsd": rmsd,
                                    "mapped_residues": len(xyz)})
    evidence = {"schema_version": 1, "run_id": plan["identity"], "real_inference": True,
                "tasks": runtime_records, "predictions": len(rows), "internal_artifacts_hashed": internal,
                "external_msa_receipt_entries": external,
                "external_msa_note": "Server-validated by controller; not rehashed by this local verifier.",
                "independent_checks": independent, "retained_backbone_checks": fixed_checks,
                "archive_sha256": digest(bundle)}
    write_json(Path(output) if output is not None else root / "acceptance_verification.json", evidence)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--reference", help="Relocated original input; must match the recorded SHA-256")
    parser.add_argument("--output", help="Write fresh verification outside an immutable archived run")
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, args.reference, args.output), indent=2))
