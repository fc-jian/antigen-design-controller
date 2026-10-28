from copy import deepcopy
from pathlib import Path
import numpy as np
import pytest
from Bio.PDB import Structure, Model, Chain, Residue, Atom, PDBIO
from antigen_design.config import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    c = deepcopy(DEFAULTS)
    c["input"] = str(ROOT / "projects/1ubq/inputs/1ubq.cif")
    c["output"] = str(tmp_path / "run")
    c["selection"]["regions"] = ["A:50-60"]
    c["execution"].update(cpu_host="cluster-cpu", gpu_host="cluster-gpu")
    return c


@pytest.fixture
def synthetic(tmp_path):
    s = Structure.Structure("test")
    m = Model.Model(0)
    s.add(m)
    serial = 1
    for chain_index, chain_id in enumerate(["A", "B"]):
        chain = Chain.Chain(chain_id)
        m.add(chain)
        for i, (number, insertion) in enumerate([(10, " "), (11, " "), (11, "A"), (14, " "), (15, " ")]):
            r = Residue.Residue((" ", number, insertion), "ALA", " ")
            chain.add(r)
            for j, name in enumerate(["N", "CA", "C", "O", "CB"]):
                xyz = np.array([i * 3.8 + j * 0.4, np.sin(i) + j * 0.6, chain_index * 3 + j * 0.4], float)
                r.add(Atom.Atom(name, xyz, 20.0, 1.0, " ", f"{name:>4}", serial, element=name[0]))
                serial += 1
    path = tmp_path / "fixture.pdb"
    writer = PDBIO()
    writer.set_structure(s)
    writer.save(str(path))
    return path
