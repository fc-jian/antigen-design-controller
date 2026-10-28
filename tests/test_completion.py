from copy import deepcopy
from pathlib import Path
import getpass
import numpy as np
import pytest
from antigen_design import remote_control
from antigen_design.config import build_plan, validate_config
from antigen_design.io import read_json, write_json
from antigen_design.legacy_import import import_results
from antigen_design.screening import evaluate
from antigen_design.structure import Protein
from antigen_design.structure_diversity import distances
from test_p3 import row, policy
from test_pipeline import make_plan


def test_structural_diversity_requires_evidence_and_filters(policy):
    policy["diversity"]["min_structure_rmsd"] = .5
    rows = [row(), row("b", sequence="AAAC")]
    expected = {"a": [("1", "0")], "b": [("1", "0")]}
    with pytest.raises(ValueError, match="validated structures"):
        evaluate(rows, expected, policy)
    result = evaluate(rows, expected, policy, lambda a, b: .1)
    assert result[1]["reasons"] == ["structural_diversity"]
    assert all(r["decision"] == "selected" for r in evaluate(rows, expected, policy, lambda a, b: None))


def test_structure_distances_respect_mapping_and_rigid_motion(config, tmp_path):
    plan = build_plan(config)
    plan["backbones"].append(deepcopy(plan["backbones"][0]))
    plan["backbones"][1]["id"] = "b0001"
    plan["backbones"][1]["mapping"][0]["design"] = True
    root = Path(config["output"]); root.mkdir()
    a, b = root / "a.pdb", root / "b.pdb"
    protein = Protein(config["input"])
    protein.normalized(a)
    for residue in protein.residues:
        for atom in residue:
            atom.coord += [10, 20, 30]
    protein.normalized(b)
    rows = [{"status": "succeeded", "candidate_id": cid, "backbone": bid,
             "prediction_id": cid, "prediction": str(path)}
            for cid, bid, path in [("a", "b0000", a), ("b", "b0000", b), ("c", "b0001", a)]]
    distance, evidence = distances(root, plan, rows)
    assert distance("a", "b") < .001
    assert distance("a", "c") is None
    assert len(evidence["prediction_sha256"]) == 3


def test_explicit_import_recomputes_metrics_and_rejects_bad_mapping(config, tmp_path):
    plan = build_plan(config)
    record = {"id": "ubiquitin-only", "prediction": config["input"], "model": "reference-self-check",
              "mapping": plan["backbones"][0]["mapping"], "reference_design": plan["selected"],
              "legacy_metadata": {"rmsd": 999}}
    manifest = {"schema_version": 1, "reference": config["input"], "records": [record]}
    path = tmp_path / "manifest.json"; write_json(path, manifest)
    result = import_results(path, tmp_path / "import")
    assert result["results"][0]["metrics"]["all_rmsd_fit_all"] < 1e-6
    assert result["results"][0]["legacy_metadata"]["rmsd"] == 999
    record["mapping"][0]["reference"] = 1
    write_json(path, manifest)
    with pytest.raises(ValueError):
        import_results(path, tmp_path / "invalid")


@pytest.mark.parametrize("wrong_field", [1, 2, 3])
def test_reconcile_exact_identity_prevents_wrong_job(config, monkeypatch, wrong_field):
    plan, state = make_plan(config)
    name = "mpnn_b0000"
    record = Path(config["output"]) / "control" / f"{name}_a1.json"
    write_json(record, {"state": "submitting"})
    expected = f"123|ah_{plan['identity'][:8]}_{name}_a1|{config['output']}|{getpass.getuser()}|\n"
    wrong = expected.split("|")
    wrong[wrong_field] = "wrong_" + wrong[wrong_field]
    monkeypatch.setattr(remote_control.subprocess, "check_output", lambda *a, **k: "|".join(wrong))
    with pytest.raises(ValueError, match="does not match"):
        remote_control.reconcile(plan, {"task": name, "attempt": 1, "job_id": "123"})
    assert "job_id" not in read_json(record)
    monkeypatch.setattr(remote_control.subprocess, "check_output", lambda *a, **k: expected)
    assert remote_control.reconcile(plan, {"task": name, "attempt": 1, "job_id": "123"})["job_id"] == "123"
    with pytest.raises(ValueError):
        remote_control.reconcile(plan, {"task": name, "attempt": 1, "job_id": "456"})


@pytest.mark.parametrize("key,value", [("gpu", 1), ("gpu_server", True), ("db_load_mode", 5), ("batch_size", 0)])
def test_bad_msa_config(config, key, value):
    config["msa"][key] = value
    with pytest.raises(ValueError):
        validate_config(config)
