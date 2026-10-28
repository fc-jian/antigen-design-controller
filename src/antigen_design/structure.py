from dataclasses import dataclass, asdict
from pathlib import Path
from copy import deepcopy
import re
import string

import numpy as np
from Bio.PDB import PDBParser, MMCIFParser, PDBIO, ShrakeRupley, Structure, Model, Chain
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.Data.PDBData import protein_letters_3to1
from Bio.PDB.DSSP import residue_max_acc


@dataclass
class ResidueRecord:
    index: int
    chain: str
    position: int
    number: int
    insertion: str
    amino_acid: str
    normalized_chain: str
    numbering_source: str = "author"

    @property
    def original_id(self):
        return f"{self.chain}:{self.number}{self.insertion}"


class Protein:
    def __init__(self, path):
        self.path = Path(path).resolve()
        fallback = set()
        if self.path.suffix.lower() in (".cif", ".mmcif"):
            raw = MMCIF2Dict(str(self.path))
            missing_author_numbers = "_atom_site.auth_seq_id" not in raw
            missing_author = "_atom_site.auth_seq_id" not in raw or "_atom_site.auth_asym_id" not in raw
            auth = raw.get("_atom_site.auth_seq_id", raw["_atom_site.label_seq_id"])
            labels = raw["_atom_site.label_seq_id"]
            chains = raw.get("_atom_site.auth_asym_id", raw["_atom_site.label_asym_id"])
            fixed = []
            for a, label, chain in zip(auth, labels, chains):
                if (missing_author_numbers or a in (".", "?")) and label not in (".", "?"):
                    a = label
                    fallback.add((chain, int(label)))
                fixed.append(a)
            # Per-residue fallback, preserving author chain IDs and non-polymers.
            raw["_atom_site.auth_seq_id"] = fixed
            raw["_atom_site.auth_asym_id"] = chains
            from Bio.PDB.mmcifio import MMCIFIO
            import tempfile
            if fallback or missing_author:
                with tempfile.NamedTemporaryFile(suffix=".cif", mode="w+") as temp:
                    writer = MMCIFIO()
                    writer.set_dict(raw)
                    writer.save(temp.name)
                    self.structure = MMCIFParser(QUIET=True).get_structure("input", temp.name)
            else:
                self.structure = MMCIFParser(QUIET=True).get_structure("input", str(self.path))
        else:
            self.structure = PDBParser(QUIET=True).get_structure("input", str(self.path))
        self.model = next(self.structure.get_models())
        self.records, self.residues = [], []
        self.warnings = []
        if len(list(self.structure.get_models())) > 1:
            self.warnings.append("Only the first structural model is used")
        protein_chains = [c for c in self.model if any(r.id[0] == " " for r in c)]
        if len(protein_chains) > 62:
            raise ValueError("PDB normalization supports at most 62 protein chains")
        for c, norm in zip(protein_chains, string.ascii_uppercase + string.ascii_lowercase + string.digits):
            position = 0
            for residue in c:
                if residue.id[0] != " ":
                    continue
                name = residue.resname.upper()
                if name not in protein_letters_3to1:
                    raise ValueError(f"Unsupported polymer residue {c.id}:{residue.id}: {name}")
                missing = {"N", "CA", "C", "O"} - set(residue.child_dict)
                if missing:
                    raise ValueError(f"Missing backbone atoms at {c.id}:{residue.id}: {sorted(missing)}")
                position += 1
                self.records.append(ResidueRecord(len(self.records), c.id, position, residue.id[1],
                    residue.id[2].strip(), protein_letters_3to1[name], norm,
                    "label_fallback" if (c.id, residue.id[1]) in fallback else "author"))
                self.residues.append(residue)
        if not self.records:
            raise ValueError("No standard protein residues found")
        if len({r.original_id for r in self.records}) != len(self.records):
            raise ValueError("Ambiguous residue IDs after numbering fallback")

    def mapping(self):
        return [asdict(r) | {"original_id": r.original_id} for r in self.records]

    def normalized(self, target):
        result = Structure.Structure("normalized")
        model = Model.Model(0)
        result.add(model)
        for rec, residue in zip(self.records, self.residues):
            if rec.normalized_chain not in model:
                model.add(Chain.Chain(rec.normalized_chain))
            new = deepcopy(residue)
            new.detach_parent()
            new.id = (" ", rec.position, " ")
            # Collapse alternate locations to the parser-selected conformer.
            for atom in list(new):
                if atom.is_disordered():
                    chosen = deepcopy(atom.selected_child)
                    new.detach_child(atom.id)
                    chosen.set_altloc(" ")
                    # Parser-flagged children would confuse PDBIO unpacking as plain atoms.
                    chosen.disordered_flag = 0
                    new.add(chosen)
                else:
                    atom.set_altloc(" ")
            model[rec.normalized_chain].add(new)
        writer = PDBIO()
        writer.set_structure(result)
        writer.save(str(target))

    def ca(self):
        return np.array([r["CA"].coord for r in self.residues], dtype=float)

    def surface(self, threshold=0.2, probe=1.4, points=960):
        # All protein chains jointly occlude one another; water/ligands are not protein SASA context.
        context = deepcopy(self.model)
        for chain in list(context):
            for residue in list(chain):
                if residue.id[0] != " ":
                    chain.detach_child(residue.id)
            if not len(chain):
                context.detach_child(chain.id)
        ShrakeRupley(probe_radius=probe, n_points=points).compute(context, level="R")
        values = [float(context[r.chain][res.id].sasa / residue_max_acc["Wilke"][res.resname])
                  for r, res in zip(self.records, self.residues)]
        return {i for i, value in enumerate(values) if value > threshold}, values


REGION = re.compile(r"^([^:]+):(-?\d+[A-Za-z]?)(?:-(-?\d+[A-Za-z]?))?$")


def select_region(protein, region, numbering):
    match = REGION.fullmatch(region)
    if not match:
        raise ValueError(f"Invalid region: {region}")
    chain, start, end = match.groups()
    end = end or start
    records = [r for r in protein.records if r.chain == chain]
    if numbering == "parsed":
        lookup = {str(r.position): r.index for r in records}
    else:
        lookup = {f"{r.number}{r.insertion}": r.index for r in records}
    if start not in lookup or end not in lookup:
        raise ValueError(f"Region endpoints are not present: {region} ({numbering})")
    a, b = lookup[start], lookup[end]
    if a > b:
        raise ValueError(f"Reversed region: {region}")
    return list(range(a, b + 1))


def segments(protein, selected):
    result = []
    for i in sorted(selected):
        if not result or i != result[-1][-1] + 1 or protein.records[i].chain != protein.records[result[-1][-1]].chain:
            result.append([])
        result[-1].append(i)
    return result
