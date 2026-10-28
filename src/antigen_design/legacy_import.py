"""Explicit prediction interchange: recompute mapped metrics, never trust legacy scores."""
from pathlib import Path
import numpy as np
from .io import read_json, write_json, digest, fingerprint
from .structure import Protein
from .metrics import compare
from .contracts import METRIC_VERSION


def import_results(manifest_path, output):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Import output must be new or empty")
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 1 or set(manifest) != {"schema_version", "reference", "records"}:
        raise ValueError("Import manifest requires schema_version, reference, records")
    reference_path = (manifest_path.parent / manifest["reference"]).resolve()
    reference = Protein(reference_path)
    if not isinstance(manifest["records"], list) or not manifest["records"]:
        raise ValueError("Import records must be nonempty")
    results, identities = [], set()
    for item in manifest["records"]:
        required = {"id", "prediction", "model", "mapping", "reference_design"}
        if set(item) - required - {"legacy_metadata"} or not required <= set(item):
            raise ValueError("Invalid import record fields")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in identities or not isinstance(item["model"], str) or not item["model"]:
            raise ValueError("Unique record IDs and explicit model labels are required")
        identities.add(item["id"])
        path = (manifest_path.parent / item["prediction"]).resolve()
        prediction = Protein(path)
        mapping = item["mapping"]
        selected = item["reference_design"]
        if not isinstance(selected, list) or any(type(i) is not int or not 0 <= i < len(reference.records) for i in selected) or len(set(selected)) != len(selected):
            raise ValueError("Invalid reference_design indices")
        if not isinstance(mapping, list) or len(mapping) != len(prediction.records):
            raise ValueError("Import needs a complete explicit residue mapping")
        lookup = {(r.chain, r.position): (r, xyz) for r, xyz in zip(prediction.records, prediction.ca())}
        ids = [(m["chain"], m["position"]) for m in mapping]
        if len(set(ids)) != len(ids) or set(ids) != set(lookup):
            raise ValueError("Import topology does not match mapping")
        for m in mapping:
            idx = m["reference"]
            if type(m["design"]) is not bool or (idx is not None and (type(idx) is not int or not 0 <= idx < len(reference.records))):
                raise ValueError("Invalid mapping value")
            if not m["design"]:
                if idx is None or idx in selected or lookup[(m["chain"], m["position"])][0].amino_acid != reference.records[idx].amino_acid:
                    raise ValueError("Imported retained sequence/mapping mismatch")
            elif idx is not None and idx not in selected:
                raise ValueError("Design mapping conflicts with reference_design")
        metrics = compare(reference.ca(), np.array([lookup[key][1] for key in ids]),
                          [m["reference"] for m in mapping], set(selected))
        results.append({"source_id": item["id"], "declared_model": item["model"],
            "prediction_sha256": digest(path), "mapping_sha256": fingerprint(mapping),
            "metric_version": METRIC_VERSION, "metrics": metrics,
            "legacy_metadata": item.get("legacy_metadata", {}),
            "provenance": "Imported structure; model execution and seed are not independently attested."})
    payload = {"schema_version": 1, "reference_sha256": digest(reference_path),
               "manifest_sha256": digest(manifest_path), "results": results,
               "scope": "Explicit files only; metrics recomputed, no legacy score relabelling or inference claim."}
    write_json(output / "imported_evaluations.json", payload)
    return payload
