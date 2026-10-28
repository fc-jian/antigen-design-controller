"""Explicit engineering gates, Pareto fronts and deterministic sequence diversity.

Scores describe structural consistency only. Confidence scales are model-specific.
Every planned prediction is required; failed or absent observations cannot improve rank.
"""
import csv
import math
from collections import defaultdict, Counter
from pathlib import Path
from .io import read_json, write_json, digest, fingerprint


def validate_policy(p):
    if set(p) != {"schema_version", "name", "model", "metric_version", "gates", "objectives", "diversity"}:
        raise ValueError("Policy requires exactly schema_version/name/model/metric_version/gates/objectives/diversity")
    if p["schema_version"] != 1 or not all(isinstance(p[k], str) and p[k] for k in ("name", "model", "metric_version")):
        raise ValueError("Invalid policy identity")
    if not isinstance(p["gates"], dict) or not p["gates"] or not isinstance(p["objectives"], dict) or not p["objectives"]:
        raise ValueError("Policy needs gates and objectives")
    for key, gate in p["gates"].items():
        if not isinstance(gate, dict) or not gate or set(gate) - {"min", "max"}:
            raise ValueError(f"Invalid gate: {key}")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in gate.values()):
            raise ValueError(f"Nonfinite gate: {key}")
        if gate.get("min", -math.inf) > gate.get("max", math.inf):
            raise ValueError(f"Reversed gate: {key}")
    if any(v not in ("min", "max") for v in p["objectives"].values()):
        raise ValueError("Objective direction must be min or max")
    d = p["diversity"]
    if not isinstance(d, dict) or not {"max_candidates", "min_sequence_distance"} <= set(d) or set(d) - {"max_candidates", "min_sequence_distance", "min_structure_rmsd"}:
        raise ValueError("Invalid diversity configuration")
    if type(d["max_candidates"]) is not int or d["max_candidates"] < 1:
        raise ValueError("max_candidates must be positive")
    if type(d["min_sequence_distance"]) not in (int, float) or not 0 <= d["min_sequence_distance"] <= 1:
        raise ValueError("min_sequence_distance must be in [0,1]")
    if "min_structure_rmsd" in d and (type(d["min_structure_rmsd"]) not in (int, float) or not math.isfinite(d["min_structure_rmsd"]) or d["min_structure_rmsd"] < 0):
        raise ValueError("min_structure_rmsd must be finite and nonnegative")


def sequence_distance(a, b):
    """Normalized edit distance, chain by chain; different topology is maximally distant."""
    a, b = a.split(":"), b.split(":")
    if len(a) != len(b):
        return 1.0
    distance = length = 0
    for x, y in zip(a, b):
        previous = list(range(len(y) + 1))
        for i, aa in enumerate(x, 1):
            current = [i]
            for j, bb in enumerate(y, 1):
                current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (aa != bb)))
            previous = current
        distance += previous[-1]
        length += max(len(x), len(y))
    return distance / max(1, length)


def pareto_fronts(candidates, objectives):
    remaining = list(candidates)
    front = 0
    def dominates(a, b):
        pairs = [(a["objectives"][k], b["objectives"][k], direction) for k, direction in objectives.items()]
        return (all(x <= y if d == "min" else x >= y for x, y, d in pairs)
                and any(x < y if d == "min" else x > y for x, y, d in pairs))
    while remaining:
        layer = [a for a in remaining if not any(dominates(b, a) for b in remaining)]
        for a in layer:
            a["pareto_front"] = front
        remaining = [a for a in remaining if a not in layer]
        front += 1


def evaluate(rows, expected, policy, structure_distance=None):
    validate_policy(policy)
    if "min_structure_rmsd" in policy["diversity"] and structure_distance is None:
        raise ValueError("Structural diversity requires validated structures")
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        cid = row.get("candidate_id")
        if not cid or cid not in expected:
            # Failed summary rows may have no candidate identity. Expected slots below remain missing.
            if row.get("status") == "succeeded":
                raise ValueError("Unexpected successful candidate in summary")
            continue
        pid = row.get("prediction_id")
        if not pid or pid in seen:
            raise ValueError("Missing or duplicate prediction identity")
        seen.add(pid)
        groups[cid].append(row)
    results = []
    for cid, slots in sorted(expected.items()):
        observations = groups[cid]
        reasons, failures = [], []
        actual = [(str(r.get("fold_seed")), str(r.get("sample"))) for r in observations]
        if len(actual) != len(set(actual)) or set(actual) != set(slots):
            reasons.append("missing_or_duplicate_prediction_slots")
        seqs = {r.get("sequence", "") for r in observations}
        if len(seqs) != 1 or not next(iter(seqs), ""):
            reasons.append("missing_or_inconsistent_sequence")
        values = defaultdict(list)
        for row in observations:
            if row.get("status") != "succeeded":
                reasons.append("pipeline_not_succeeded")
            if row.get("prediction_model") != policy["model"] or row.get("metric_version") != policy["metric_version"]:
                reasons.append("incompatible_model_or_metric_version")
            for key in policy["gates"].keys() | policy["objectives"].keys():
                try:
                    value = float(row.get(key, ""))
                    if not math.isfinite(value):
                        raise ValueError()
                    values[key].append(value)
                    gate = policy["gates"].get(key, {})
                    if not gate.get("min", -math.inf) <= value <= gate.get("max", math.inf):
                        failures.append("gate_failed:" + key)
                except (ValueError, TypeError):
                    reasons.append("missing_or_nonfinite:" + key)
        decision = "insufficient_evidence" if reasons else "rejected" if failures else "eligible"
        results.append({"candidate_id": cid, "decision": decision, "reasons": sorted(set(reasons + failures)),
            "prediction_ids": sorted(r["prediction_id"] for r in observations),
            "observed_predictions": len(observations), "expected_predictions": len(slots),
            "sequence": next(iter(seqs)) if len(seqs) == 1 else "",
            "objectives": {k: (max(values[k]) if d == "min" else min(values[k]))
                           for k, d in policy["objectives"].items() if values[k]},
            "ranges": {k: {"min": min(v), "max": max(v)} for k, v in values.items() if v}})
    eligible = [r for r in results if r["decision"] == "eligible"]
    pareto_fronts(eligible, policy["objectives"])
    selected = []
    # Within each front use farthest-first selection; ties resolved by provenance ID.
    for front in sorted({r["pareto_front"] for r in eligible}):
        pool = [r for r in eligible if r["pareto_front"] == front]
        while pool:
            def distance(r):
                return min((sequence_distance(r["sequence"], s["sequence"]) for s in selected), default=1.0)
            item = sorted(pool, key=lambda r: (-distance(r), r["candidate_id"]))[0]
            pool.remove(item)
            nearest = distance(item)
            duplicate = any(item["sequence"] == s["sequence"] for s in selected)
            structural = [structure_distance(item["candidate_id"], s["candidate_id"]) for s in selected] if structure_distance else []
            redundant_structure = any(v is not None and v < policy["diversity"].get("min_structure_rmsd", 0) for v in structural)
            if duplicate or nearest < policy["diversity"]["min_sequence_distance"]:
                item.update(decision="redundant", reasons=["sequence_diversity"], nearest_sequence_distance=nearest)
            elif redundant_structure:
                item.update(decision="redundant", reasons=["structural_diversity"],
                            nearest_structure_rmsd=min(v for v in structural if v is not None))
            elif len(selected) >= policy["diversity"]["max_candidates"]:
                item.update(decision="not_selected", reasons=["selection_limit"])
            else:
                item.update(decision="selected", selection_order=len(selected), nearest_sequence_distance=nearest)
                selected.append(item)
    for result in results:
        result.pop("sequence")  # preserve provenance without duplicating sequences in selection reports
    return results


def screen(root, policy_path):
    root = Path(root)
    policy = read_json(policy_path)
    plan = read_json(root / "plan.json")
    expected, chain_lengths = {}, {}
    for b in plan["backbones"]:
        for i in range(plan["config"]["mpnn"]["sequences"]):
            cid = fingerprint({"run": plan["identity"], "backbone": b["id"], "sample": i})
            chain_lengths[cid] = list(Counter(m["chain"] for m in b["mapping"]).values())
            expected[cid] = [(str(seed), str(sample)) for seed in plan["config"]["folding"]["seeds"]
                             for sample in range(plan["config"]["folding"]["samples"])]
    with open(root / "summary.csv", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        if row.get("candidate_id") in chain_lengths and row.get("sequence"):
            lengths = chain_lengths[row["candidate_id"]]
            sequence = row["sequence"]
            if len(sequence) != sum(lengths) or set(sequence) - set("ACDEFGHIKLMNPQRSTVWY"):
                raise ValueError("Summary sequence does not match planned chain lengths/alphabet")
            start, chains = 0, []
            for length in lengths:
                chains.append(sequence[start:start + length])
                start += length
            row["sequence"] = ":".join(chains)
    structure_distance, structural_evidence = None, None
    if "min_structure_rmsd" in policy["diversity"]:
        from .structure_diversity import distances
        structure_distance, structural_evidence = distances(root, plan, rows)
    results = evaluate(rows, expected, policy, structure_distance)
    payload = {"schema_version": 1, "policy": policy, "policy_sha256": fingerprint(policy),
        "summary_sha256": digest(root / "summary.csv"), "plan_sha256": digest(root / "plan.json"),
        "run_id": plan["identity"], "results": results,
        "counts": dict(Counter(r["decision"] for r in results)),
        "aggregation": "Every planned prediction required; worst observation per objective; Pareto then sequence diversity",
        "scope": "Structural engineering screening only; thresholds are explicit assumptions, not validated efficacy criteria."}
    if structural_evidence is not None:
        payload["structural_diversity"] = structural_evidence
    output = root / ("selection-" + fingerprint(policy)[:16] + ".json")
    write_json(output, payload)
    return payload
