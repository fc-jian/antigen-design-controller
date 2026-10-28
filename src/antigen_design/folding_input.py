"""Strict, explicit AF3/AlphaFold Server/Protenix protein-plus input conversion.

MSA/template filesystem paths and custom CCD definitions are intentionally rejected.
The original input and normalized input are both retained by the web application.
"""
from copy import deepcopy
import json
import re
from pathlib import Path

KINDS = {"protein": "proteinChain", "dna": "dnaSequence", "rna": "rnaSequence"}
ALPHABETS = {"proteinChain": set("ACDEFGHIKLMNPQRSTVWYX"),
             "dnaSequence": set("ACGTN"), "rnaSequence": set("ACGUN")}


def keys(value, allowed, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object")
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError(f"{where}: unsupported fields {sorted(extra)}; input was not discarded")


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def ccd(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:CCD_)?[A-Za-z0-9]+", value):
        raise ValueError("Invalid CCD identifier")
    return value.removeprefix("CCD_")


def chain_id(index):
    result = ""
    while True:
        result += chr(65 + index % 26)
        index = index // 26 - 1
        if index < 0:
            return result


def parse_input(text, default_seeds=None):
    default_seeds = default_seeds or [101]
    if text.lstrip().startswith(("{", "[")):
        raw = json.loads(text)
        jobs = raw if isinstance(raw, list) else [raw]
    else:
        jobs, title, seq = [], "sequence-1", []
        def add():
            if seq:
                jobs.append({"name": title, "sequences": [
                    {"proteinChain": {"sequence": "".join(seq), "count": 1}}]})
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(">"):
                add()
                title, seq = line[1:].strip() or f"sequence-{len(jobs)+1}", []
            elif line:
                seq.append(line)
        add()
    if not jobs or len(jobs) > 100:
        raise ValueError("Supply 1–100 jobs (FASTA records are independent predictions)")
    normalized = [normalize(job, i, default_seeds) for i, job in enumerate(jobs)]
    if sum(sum(len(next(iter(e.values())).get("sequence", "")) *
                   next(iter(e.values()))["count"] for e in j["sequences"]) for j in normalized) > 100000:
        raise ValueError("Batch exceeds 100,000 polymer residues")
    return normalized


def normalize(job, index, default_seeds):
    keys(job, {"name", "sequences", "modelSeeds", "dialect", "version",
               "bondedAtomPairs", "covalent_bonds"}, "job")
    dialect = job.get("dialect")
    if dialect not in (None, "alphafold3", "alphafoldserver", "protenix"):
        raise ValueError("Unsupported JSON dialect")
    if "version" in job and job["version"] not in (1, 2, 3, 4):
        raise ValueError("Unsupported JSON version")
    af3 = dialect == "alphafold3"
    seeds = job.get("modelSeeds", default_seeds)
    if not isinstance(seeds, list) or not seeds or len(seeds) > 16:
        raise ValueError("modelSeeds requires 1–16 explicit seeds (empty/random seeds are not supported)")
    for seed in seeds:
        integer(seed, 0, 2**31 - 1, "seed")
    if len(set(seeds)) != len(seeds):
        raise ValueError("Duplicate modelSeeds")
    entities = job.get("sequences")
    if not isinstance(entities, list) or not entities or len(entities) > 64:
        raise ValueError("sequences requires 1–64 entities")
    result, ids, next_id = [], {}, 0
    for ei, entity in enumerate(entities, 1):
        if not isinstance(entity, dict) or len(entity) != 1:
            raise ValueError("Each entity requires exactly one molecular type")
        source_kind, source = next(iter(entity.items()))
        kind = KINDS.get(source_kind, source_kind)
        if kind not in {*ALPHABETS, "ligand", "ion"}:
            raise ValueError(f"Unsupported entity type: {source_kind}")
        allowed = {"id", "sequence", "modifications"} if kind in ALPHABETS else (
            {"id", "ccdCodes", "smiles"} if af3 and kind == "ligand" else {"id", kind})
        keys(source, allowed | (set() if af3 else {"count"}), f"entity {ei}")
        raw_ids = source.get("id")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        count = len(raw_ids) if af3 and isinstance(raw_ids, list) else source.get("count", 1)
        integer(count, 1, 64, "count")
        if raw_ids is None:
            raw_ids = []
            for _ in range(count):
                while chain_id(next_id) in ids:
                    next_id += 1
                raw_ids.append(chain_id(next_id))
                next_id += 1
        if not isinstance(raw_ids, list) or len(raw_ids) != count:
            raise ValueError("Chain IDs must match entity copy count")
        for copy, cid in enumerate(raw_ids, 1):
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,7}", cid) or cid in ids:
                raise ValueError(f"Invalid or duplicated chain ID: {cid}")
            ids[cid] = (ei, copy)
        target = {"count": count, "id": raw_ids}
        if kind in ALPHABETS:
            sequence = source.get("sequence", "")
            if not isinstance(sequence, str) or not sequence or set(sequence) - ALPHABETS[kind]:
                raise ValueError(f"Invalid {kind} sequence; use uppercase residue codes")
            if len(sequence) > 5000:
                raise ValueError("A polymer chain exceeds 5,000 residues")
            target["sequence"] = sequence
            mods, positions = [], set()
            for mod in source.get("modifications", []):
                type_key = "ptmType" if kind == "proteinChain" else "modificationType"
                pos_key = "ptmPosition" if kind == "proteinChain" else "basePosition"
                keys(mod, {type_key, pos_key}, "modification")
                pos = integer(mod.get(pos_key), 1, len(sequence), pos_key)
                if pos in positions:
                    raise ValueError("Duplicate modification position")
                positions.add(pos)
                mods.append({type_key: "CCD_" + ccd(mod.get(type_key)), pos_key: pos})
            if mods:
                target["modifications"] = mods
        elif kind == "ligand":
            if af3:
                if ("ccdCodes" in source) == ("smiles" in source):
                    raise ValueError("Ligand needs exactly one of ccdCodes or smiles")
                codes = source.get("ccdCodes")
                if codes is not None and (not isinstance(codes, list) or not codes):
                    raise ValueError("ccdCodes must be a nonempty list")
                ligand = "CCD_" + "_".join(ccd(c) for c in codes) if codes else source["smiles"]
            else:
                ligand = source.get("ligand")
            if not isinstance(ligand, str) or not ligand or len(ligand) > 10000 or ligand.startswith("FILE_") or any(c.isspace() for c in ligand):
                raise ValueError("Ligand requires a CCD code or SMILES; file paths are not accepted")
            if ligand.startswith("CCD_"):
                for code in ligand[4:].split("_"):
                    ccd(code)
            target["ligand"] = ligand
        else:
            target["ion"] = ccd(source.get("ion"))
        result.append({kind: target})
    bonds = []
    if "bondedAtomPairs" in job and "covalent_bonds" in job:
        raise ValueError("Use only one bond dialect")
    for pair in job.get("bondedAtomPairs", []):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("bondedAtomPairs requires atom pairs")
        bond = {}
        for side, atom in enumerate(pair, 1):
            if not isinstance(atom, list) or len(atom) != 3 or atom[0] not in ids:
                raise ValueError("Invalid bonded atom chain")
            ei, copy = ids[atom[0]]
            bond.update({f"entity{side}": str(ei), f"copy{side}": copy,
                         f"position{side}": str(atom[1]), f"atom{side}": atom[2]})
        bonds.append(bond)
    bonds.extend(deepcopy(job.get("covalent_bonds", [])))
    for bond in bonds:
        keys(bond, {f"{key}{s}" for s in (1, 2) for key in ("entity", "copy", "position", "atom")}, "bond")
        for s in (1, 2):
            try:
                ei, pos = int(bond[f"entity{s}"]), int(bond[f"position{s}"])
            except (KeyError, ValueError, TypeError):
                raise ValueError("Invalid bond entity/position") from None
            integer(ei, 1, len(result), "bond entity")
            kind, entity = next(iter(result[ei - 1].items()))
            length = len(entity["sequence"]) if "sequence" in entity else (
                len(entity.get("ligand", "")[4:].split("_")) if entity.get("ligand", "").startswith("CCD_") else 1)
            integer(pos, 1, length, "bond position")
            integer(bond.get(f"copy{s}", 1), 1, entity["count"], "bond copy")
            if not isinstance(bond.get(f"atom{s}"), (str, int)) or not str(bond[f"atom{s}"]):
                raise ValueError("Invalid bond atom")
            bond[f"entity{s}"], bond[f"position{s}"] = str(ei), str(pos)
        if ("copy1" in bond) != ("copy2" in bond):
            raise ValueError("Specify both bond copies")
    name = job.get("name", f"job-{index+1}")
    if not isinstance(name, str) or not name or len(name) > 160:
        raise ValueError("Job name must be 1–160 characters")
    return {"name": f"input_{index:04d}", "display_name": name, "seeds": seeds,
            "sequences": result, "covalent_bonds": bonds}


def build_fold_plan(config):
    # Input on disk is the original JSON/FASTA, not a trusted normalized plan.
    jobs = parse_input(Path(config["input"]).read_text(), config["folding"]["seeds"])
    records = []
    for job in jobs:
        chains = {cid: e["proteinChain"]["sequence"] for e in job["sequences"] if "proteinChain" in e
                  for cid in e["proteinChain"]["id"]}
        records.append({"id": job["name"], "chains": chains, "job": job,
                        "sequence": ":".join(chains.values())})
    return {"config": config, "mode": "folding", "fold_records": records,
            "backbones": [], "reference_mapping": [], "selected": [],
            "warnings": ["RNA/DNA do not receive an MSA from the configured protein ColabFold search.",
                         "Custom CCD, templates and supplied MSA fields are rejected rather than ignored."],
            "expected_predictions": sum(len(j["seeds"]) for j in jobs) * config["folding"]["samples"]}
