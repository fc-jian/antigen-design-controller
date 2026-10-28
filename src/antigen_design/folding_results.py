"""Prediction identity checks and confidence parsing shared by workers and web UI."""
import math
from pathlib import Path
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.Data.PDBData import protein_letters_1to3
from .io import read_json, write_json, fingerprint


def cif_residues(path):
    raw = MMCIF2Dict(str(path))
    chains = raw["_atom_site.label_asym_id"]
    positions = raw["_atom_site.label_seq_id"]
    authors = raw.get("_atom_site.auth_seq_id", positions)
    comp = raw["_atom_site.label_comp_id"]
    atoms = raw["_atom_site.label_atom_id"]
    scores = raw.get("_atom_site.B_iso_or_equiv", ["?"] * len(chains))
    coords = [raw[f"_atom_site.Cartn_{a}"] for a in "xyz"]
    rows = {}
    for i, chain in enumerate(chains):
        if any(not math.isfinite(float(c[i])) for c in coords):
            raise ValueError("Nonfinite prediction coordinates")
        position = positions[i] if positions[i] not in (".", "?") else authors[i]
        key = (chain, position, comp[i])
        row = rows.setdefault(key, {"chain": chain, "position": position, "resname": comp[i],
                                   "atoms": [], "plddt_values": []})
        row["atoms"].append(atoms[i])
        try:
            score = float(scores[i])
            if math.isfinite(score) and 0 <= score <= 100:
                row["plddt_values"].append(score)
        except ValueError:
            pass
    if not rows:
        raise ValueError("Prediction contains no atoms")
    for row in rows.values():
        values = row.pop("plddt_values")
        row["plddt"] = sum(values) / len(values) if values else None
    return list(rows.values())


def validate_structure(path, job):
    rows = cif_residues(path)
    observed = {}
    for row in rows:
        observed.setdefault(row["chain"], []).append(row)
    expected = {cid: (kind, entity) for wrapper in job["sequences"]
                for kind, entity in wrapper.items() for cid in entity["id"]}
    if set(observed) != set(expected):
        raise ValueError("Output chain IDs do not match input entities")
    for cid, (kind, entity) in expected.items():
        actual = observed[cid]
        if "sequence" in entity:
            seq = entity["sequence"]
            if len(actual) != len(seq):
                raise ValueError(f"Output residue count mismatch for {cid}")
            position_key = "ptmPosition" if kind == "proteinChain" else "basePosition"
            type_key = "ptmType" if kind == "proteinChain" else "modificationType"
            mods = {m[position_key]: m[type_key].removeprefix("CCD_") for m in entity.get("modifications", [])}
            by_position = {r["position"]: r for r in actual}
            for pos, letter in enumerate(seq, 1):
                expected_name = (protein_letters_1to3.get(letter, "UNK").upper() if kind == "proteinChain"
                                 else ("D" + letter if kind == "dnaSequence" else letter))
                expected_name = mods.get(pos, expected_name)
                row = by_position.get(str(pos))
                if row is None or row["resname"] != expected_name:
                    raise ValueError(f"Output chemical identity mismatch at {cid}:{pos}")
        elif kind == "ion":
            if len(actual) != 1 or actual[0]["resname"] != entity["ion"]:
                raise ValueError("Output ion identity mismatch")
        elif entity["ligand"].startswith("CCD_"):
            expected_codes = entity["ligand"][4:].split("_")
            if [r["resname"] for r in actual] != expected_codes:
                raise ValueError("Output ligand CCD identity mismatch")
        elif len(actual) != 1:
            raise ValueError("SMILES ligand must produce one residue")
    return rows


def summary(path):
    value = read_json(path)
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("Confidence summary must be an object")
    return {k: v for k, v in value.items() if isinstance(v, (str, bool)) or
            isinstance(v, (float, int)) and math.isfinite(v)}


def annotate_design(result, plan):
    """Read-only display annotation from the exact frozen backbone map, never mutations."""
    from copy import deepcopy
    result = deepcopy(result)
    backbone_id = result.get("metrics", {}).get("backbone")
    matches = [b for b in plan.get("backbones", []) if b.get("id") == backbone_id]
    mapping = matches[0].get("mapping", []) if len(matches) == 1 and backbone_id is not None else []
    lookup = {(m["chain"], str(m["position"])): m for m in mapping}
    available = bool(mapping) and len(lookup) == len(mapping)
    residues = result.get("residues", [])
    available = available and len(residues) == len(mapping) and all(
        (r["chain"], str(r["position"])) in lookup for r in residues)
    result["design_mapping_available"] = bool(available)
    for residue in residues:
        entry = lookup.get((residue["chain"], str(residue["position"]))) if available else None
        residue["is_designed"] = entry["design"] if entry is not None else None
    result["design_mapping_source"] = "frozen backbone mapping" if available else None
    return result


def metrics(plan, task, directory):
    record = plan["fold_records"][task["record"]]
    predictions = read_json(Path(plan["config"]["output"]) / "tasks" / task["deps"][0] / "predictions.json")
    rows = []
    for item in predictions:
        residues = validate_structure(item["path"], record["job"])
        rows.append({"design": record["id"], "name": record["job"]["display_name"],
                     "prediction_id": fingerprint({"plan": plan["identity"], "record": record["id"],
                                                   "seed": task["seed"], "sample": item["sample"]}),
                     "fold_seed": task["seed"], "sample": item["sample"], "status": "succeeded",
                     "model": "protenix-v2", "prediction": item["path"], "confidence_path": item["confidence"],
                     "residue_count": len(residues),
                     **{"confidence_" + k: v for k, v in summary(item["confidence"]).items()}})
    write_json(directory / "metrics.json", rows)
