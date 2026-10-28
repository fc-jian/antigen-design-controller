from copy import deepcopy


import pytest


from Bio.PDB.mmcifio import MMCIFIO


from Bio.PDB.MMCIF2Dict import MMCIF2Dict


from antigen_design.structure import Protein, select_region, segments


from antigen_design.config import build_plan, validate_config, load_config


def test_altloc_collapse(tmp_path):
    import numpy as np
    from Bio.PDB import Structure, Model, Chain, Residue, Atom, PDBIO
    chain = Chain.Chain("A")
    model = Model.Model(0)
    model.add(chain)
    structure = Structure.Structure("altloc")
    structure.add(model)
    residue = Residue.Residue((" ", 1, " "), "ASN", " ")
    chain.add(residue)
    serial = 1
    for name in ("N", "C", "O"):
        residue.add(Atom.Atom(name, np.array([serial, 0.0, 0.0]), 20.0, 1.0, " ", f"{name:>4}", serial, element=name[0]))
        serial += 1
    disordered = Atom.DisorderedAtom("CA")
    for altloc, occupancy, x in (("A", 0.6, 1.0), ("B", 0.4, 9.0)):
        disordered.disordered_add(Atom.Atom("CA", np.array([x, 1.0, 1.0]), 20.0, occupancy, altloc, " CA ", serial, element="C"))
        serial += 1
    residue.add(disordered)
    source = tmp_path / "disordered.pdb"
    writer = PDBIO()
    writer.set_structure(structure)
    writer.save(str(source))
    p = Protein(source)
    assert p.residues[0]["CA"].is_disordered() == 2
    path = tmp_path / "normalized.pdb"
    p.normalized(path)
    q = Protein(path)
    ca = q.residues[0]["CA"]
    assert not ca.is_disordered() and ca.coord[0] == pytest.approx(1.0)


def test_insertions_gaps_multichain(synthetic, tmp_path):
    p = Protein(synthetic)
    assert select_region(p, "A:11-14", "original") == [1, 2, 3]
    assert select_region(p, "A:11A", "original") == [2]
    assert select_region(p, "B:2-4", "parsed") == [6, 7, 8]
    assert segments(p, {0, 2, 4, 5, 6}) == [[0], [2], [4], [5, 6]]
    with pytest.raises(ValueError, match="endpoints"):
        select_region(p, "A:12-14", "original")
    path = tmp_path / "normalized.pdb"
    p.normalized(path)
    q = Protein(path)
    assert [r.number for r in q.records] == list(range(1, 6)) * 2
    assert q.ca() == pytest.approx(p.ca(), abs=0.001)


def test_cif_label_fallback(synthetic, tmp_path):
    p = Protein(synthetic)
    path = tmp_path / "fallback.cif"
    writer = MMCIFIO()
    writer.set_structure(p.structure)
    writer.save(str(path))
    data = MMCIF2Dict(str(path))
    for i, number in enumerate(data["_atom_site.auth_seq_id"]):
        if number == "15":
            data["_atom_site.auth_seq_id"][i] = "?"
    writer.set_dict(data)
    writer.save(str(path))
    result = Protein(path)
    assert sum(r.numbering_source == "label_fallback" for r in result.records) == 2
    assert result.records[4].number == 5
    assert result.records[3].number == 14
    data.pop("_atom_site.auth_seq_id")
    writer.set_dict(data)
    writer.save(str(path))
    result = Protein(path)
    assert all(r.numbering_source == "label_fallback" for r in result.records)


@pytest.mark.parametrize("key,value", [("samples", 0), ("samples", True)])
def test_bad_counts(config, key, value):
    config["folding"][key] = value
    with pytest.raises(ValueError):
        validate_config(config)


def test_strict_model_and_unknown_keys(config, tmp_path):
    config["folding"]["model"] = "protenix_base_default_v1.0.0"
    with pytest.raises(ValueError, match="fallback"):
        validate_config(config)
    p = tmp_path / "bad.yaml"
    p.write_text("rfd3:\n  samplse: 3\n")
    with pytest.raises(ValueError, match="Unknown"):
        load_config(p)
