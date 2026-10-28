from copy import deepcopy
import pytest
from antigen_design.io import read_json
from antigen_design.screening import evaluate, sequence_distance, validate_policy
from antigen_design.pipeline import task_graph
from antigen_design.config import build_plan, validate_config
from antigen_design.stages import selected_stages, check_prerequisites


@pytest.fixture
def policy():
    return {"schema_version": 1, "name": "test-only", "model": "test-model", "metric_version": "test-v1",
            "gates": {"coverage": {"min": 1}, "rmsd": {"max": 2}},
            "objectives": {"rmsd": "min", "lddt": "max"},
            "diversity": {"max_candidates": 3, "min_sequence_distance": 0.1}}


def row(cid="a", seed=1, **kw):
    return dict(candidate_id=cid, prediction_id=f"{cid}-{seed}", fold_seed=str(seed), sample="0",
                status="succeeded", prediction_model="test-model", metric_version="test-v1",
                sequence="AAAA", coverage=1, rmsd=1, lddt=.9) | kw


def test_missing_failed_nan_and_no_best_seed_cherry_pick(policy):
    slots = {"a": [("1", "0"), ("2", "0")]}
    assert evaluate([row()], slots, policy)[0]["decision"] == "insufficient_evidence"
    for bad in (float("nan"), float("inf"), "", None):
        out = evaluate([row(), row(seed=2, rmsd=bad)], slots, policy)[0]
        assert out["decision"] == "insufficient_evidence"
    out = evaluate([row(), row(seed=2, rmsd=3)], slots, policy)[0]
    assert out["decision"] == "rejected" and out["objectives"]["rmsd"] == 3
    assert evaluate([row(), row(seed=2, status="failed")], slots, policy)[0]["decision"] == "insufficient_evidence"


def test_duplicates_models_and_slots(policy):
    slots = {"a": [("1", "0")]}
    with pytest.raises(ValueError, match="duplicate"):
        evaluate([row(), row()], slots, policy)
    assert evaluate([row(prediction_model="other")], slots, policy)[0]["decision"] == "insufficient_evidence"
    assert evaluate([row(seed=2)], slots, policy)[0]["decision"] == "insufficient_evidence"
    assert evaluate([], slots, policy)[0]["decision"] == "insufficient_evidence"


def test_pareto_diversity_and_provenance(policy):
    rows = [row(), row("b", sequence="AAAC", rmsd=.9, lddt=.8), row("c", rmsd=1.5, lddt=.8)]
    slots = {r["candidate_id"]: [("1", "0")] for r in rows}
    out = {r["candidate_id"]: r for r in evaluate(rows, slots, policy)}
    assert out["a"]["pareto_front"] == out["b"]["pareto_front"] == 0
    assert out["c"]["pareto_front"] == 1 and out["c"]["decision"] == "redundant"
    assert out["a"]["decision"] == out["b"]["decision"] == "selected"
    assert evaluate(rows, slots, policy) == evaluate(list(reversed(rows)), slots, policy)
    assert "sequence" not in out["a"]


def test_distance_chain_boundaries_and_length_change():
    assert sequence_distance("AA:AA", "AAAA") == 1
    assert sequence_distance("AAAA", "AAA") == .25
    assert sequence_distance("AA:AA", "AA:AC") == .25


def test_bad_policy(policy):
    for value in (float("nan"), float("inf"), True):
        p = deepcopy(policy)
        p["gates"]["rmsd"]["max"] = value
        with pytest.raises(ValueError):
            validate_policy(p)


def test_rfd3_graph_and_explicit_boundary(config):
    config["rfd3"].update(enabled=True, samples=1)
    validate_config(config)
    plan = build_plan(config) | {"identity": "test"}
    plan["tasks"] = task_graph(plan)
    assert "rfd3" in selected_stages(until="msa")
    state = {"tasks": {k: {"state": "pending"} for k in plan["tasks"]}}
    with pytest.raises(ValueError, match="upstream"):
        check_prerequisites(plan, state, {"mpnn"})
    assert sum(t["stage"] == "rfd3" for t in plan["tasks"].values()) == 1
    config["rfd3"]["options"]["seed"] = 999
    with pytest.raises(ValueError, match="constraints"):
        validate_config(config)


def test_screen_accounts_for_planned_missing_candidates(config, tmp_path, policy):
    import csv
    from pathlib import Path
    from antigen_design.screening import screen
    from antigen_design.io import fingerprint, write_json, digest
    from antigen_design.structure import Protein
    config["mpnn"]["sequences"] = 2
    config["folding"]["seeds"] = [1]
    config["folding"]["samples"] = 1
    plan = build_plan(config) | {"identity": "test-run"}
    root = Path(config["output"])
    write_json(root / "plan.json", plan)
    write_json(tmp_path / "policy.json", policy)
    cid = fingerprint({"run": plan["identity"], "backbone": "b0000", "sample": 0})
    observed = row(cid, sequence="".join(r.amino_acid for r in Protein(config["input"]).records))
    with open(root / "summary.csv", "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(observed))
        writer.writeheader()
        writer.writerow(observed)
    out = screen(root, tmp_path / "policy.json")
    assert out["counts"] == {"selected": 1, "insufficient_evidence": 1}
    assert out["summary_sha256"] == digest(root / "summary.csv")
    assert out["policy_sha256"] == fingerprint(policy)


def test_variable_length_can_use_backbone_gate_without_imputing_reference(policy):
    policy["gates"] = {"backbone_coverage": {"min": 1}, "retained_coverage": {"min": 1}}
    policy["objectives"] = {"backbone_rmsd": "min"}
    observations = [row(backbone_coverage=1, retained_coverage=1, backbone_rmsd=.5, rmsd="")]
    out = evaluate(observations, {"a": [("1", "0")]}, policy)
    assert out[0]["decision"] == "selected"
    assert out[0]["objectives"] == {"backbone_rmsd": .5}
