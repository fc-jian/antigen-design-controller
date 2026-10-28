import numpy as np
import pytest
from antigen_design.metrics import compare, validate_fixed
from antigen_design.structure import Protein
from antigen_design.config import build_plan
from antigen_design.adapters import validate_sequence


def points():
    return np.random.default_rng(8).normal(size=(30, 3)) * 4


def test_rigid_transform():
    x = points()
    rotation, _ = np.linalg.qr(np.random.default_rng(2).normal(size=(3, 3)))
    y = x @ rotation + [5, -8, 12]
    result = compare(x, y, list(range(30)), set(range(5)))
    for region in ("all", "design", "retained"):
        assert result[f"{region}_rmsd_fit_all"] < 1e-10
        assert result[f"{region}_rmsd_fit_retained"] < 1e-10
        assert result[f"{region}_lddt"] == 1


def test_local_perturbation():
    x = points()
    y = x.copy()
    y[:5] += [3, 0, 0]
    result = compare(x, y, list(range(30)), set(range(5)))
    assert result["design_rmsd_fit_retained"] == pytest.approx(3)
    assert result["retained_rmsd_fit_retained"] < 1e-10
    assert 0 < result["design_lddt"] < result["retained_lddt"] < 1


def test_variable_coverage_and_empty_regions():
    x = points()
    y = np.concatenate([x[5:], np.zeros((8, 3))])
    result = compare(x, y, list(range(5, 30)) + [None] * 8, set(range(5)))
    assert result["all_coverage"] == pytest.approx(25 / 30)
    assert result["design_coverage"] == 0
    assert result["design_lddt"] is None
    assert result["design_rmsd_fit_all_reason"] == "no_mapped_residues"
    result = compare(x[:2], x[:2], [0, 1], set())
    assert result["all_rmsd_fit_all"] is None
    assert result["design_coverage"] is None
    result = compare(x[:1], x[:1], [0], {0})
    assert result["all_lddt"] is None


def test_fixed_constraints(config):
    plan = build_plan(config)
    p, q = Protein(config["input"]), Protein(config["input"])
    mapping = plan["backbones"][0]["mapping"]
    validate_fixed(p, q, mapping)
    seq = "".join(r.amino_acid for r in p.records)
    validate_sequence(seq[:49] + "A" * 11 + seq[60:], mapping, p)
    with pytest.raises(ValueError, match="retained"):
        validate_sequence("A" + seq[1:], mapping, p)
    q.residues[0]["CA"].coord += 1
    with pytest.raises(ValueError, match="moved"):
        validate_fixed(p, q, mapping)


def test_duplicate_mapping_rejected():
    with pytest.raises(ValueError, match="one-to-one"):
        compare(points(), points()[:2], [0, 0], set())
