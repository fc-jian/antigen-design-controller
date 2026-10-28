"""Called inside the configured model environment, never imported by the controller."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import numpy as np
from .io import write_json, read_json, digest, fingerprint
from .structure import Protein
from .metrics import validate_fixed, compare


def validate_sequence(sequence, mapping, reference):
    if len(sequence) != len(mapping) or set(sequence) - set("ACDEFGHIKLMNPQRSTVWY"):
        raise ValueError("Invalid MPNN sequence alphabet or length")
    for aa, item in zip(sequence, mapping):
        if not item["design"] and aa != reference.records[item["reference"]].amino_acid:
            raise ValueError("MPNN changed a retained residue")


def require_order(protein, mapping):
    observed = [(r.normalized_chain, r.position) for r in protein.records]
    expected = [(r["chain"], r["position"]) for r in mapping]
    if observed != expected:
        raise ValueError("Generated chain topology differs from the planned mapping")


def rfd3(plan, task, directory):
    from lightning.fabric import seed_everything
    from rfd3.engine import RFD3InferenceConfig, RFD3InferenceEngine
    from atomworks.io.utils.io_utils import to_cif_file
    c = plan["config"]
    b = plan["backbones"][task["backbone"]]
    seed_everything(b["seed"])
    spec = {"input": str(Path(c["output"]) / "reference.pdb"), "contig": b["contig"],
            "select_fixed_atoms": b["fixed_atoms"], "select_unfixed_sequence": False}
    write_json(directory / "rfd3_input.json", {b["id"]: spec})
    options = c["rfd3"]["options"]
    blocked = {"specification", "inputs", "out_dir", "diffusion_batch_size", "n_batches", "ckpt_path", "seed"}
    if blocked.intersection(options):
        raise ValueError("RFD3 options cannot override constraints, sample count or checkpoint")
    # Match the installed Foundry entrypoint: defaults live in the config
    # dataclass, while the engine constructor requires every argument explicitly.
    engine_config = dict(RFD3InferenceConfig(ckpt_path=c["rfd3"]["checkpoint"],
        diffusion_batch_size=1, seed=b["seed"], **options))
    write_json(directory / "rfd3_engine.json", engine_config | {"trainer_precision": "32-true"})
    engine = RFD3InferenceEngine(**engine_config)
    # Mixed-precision output quantizes fixed motif coordinates (observed up to
    # 0.067 Å). FP32 preserves the existing fixed-backbone validation threshold.
    engine._assign_override("trainer.precision", "32-true")
    # Foundry returns structures only when out_dir is None. Save each returned
    # output explicitly so both the atom array and native metadata are retained.
    output = engine.run(inputs=str(directory / "rfd3_input.json"), n_batches=1)
    items = [item for batch in output.values() for item in batch]
    if len(items) != 1:
        raise ValueError(f"RFD3 returned {len(items)} structures; expected 1")
    raw = directory / "raw"
    raw.mkdir(exist_ok=True)
    items[0].dump(out_dir=str(raw))
    to_cif_file(items[0].atom_array, str(directory / "generated.cif"))
    generated = Protein(directory / "generated.cif")
    require_order(generated, b["mapping"])
    validate_fixed(Protein(c["input"]), generated, b["mapping"])
    generated.normalized(directory / "backbone.pdb")
    write_json(directory / "mapping.json", b["mapping"])


def backbone_path(plan, task):
    root = Path(plan["config"]["output"])
    b = plan["backbones"][task["backbone"]]
    return root / "tasks" / f"rfd3_{b['id']}" / "backbone.pdb" if plan["config"]["rfd3"]["enabled"] else root / "reference.pdb"


def mpnn(plan, task, directory):
    from mpnn.inference_engines.mpnn import MPNNInferenceEngine
    from atomworks.io.utils.io_utils import to_cif_file
    c, b = plan["config"], plan["backbones"][task["backbone"]]
    opts = dict(c["mpnn"]["options"])
    reserved = {"designed_residues", "fixed_residues", "designed_chains", "fixed_chains", "structure_path", "batch_size", "number_of_batches", "seed", "temperature", "structure_noise"}
    if reserved.intersection(opts):
        raise ValueError("MPNN options cannot override scope, structure, seed or counts")
    checkpoint = c["mpnn"]["checkpoint"]
    engine = MPNNInferenceEngine(model_type="protein_mpnn", is_legacy_weights=True,
        checkpoint_path=None if checkpoint == "proteinmpnn" else checkpoint,
        out_directory=str(directory / "raw"), write_fasta=True, write_structures=True)
    settings = dict(opts, structure_path=str(backbone_path(plan, task)), batch_size=c["mpnn"]["sequences"],
        number_of_batches=1, seed=b["seed"], temperature=c["mpnn"]["temperature"], structure_noise=0.0,
        designed_residues=[f"{m['chain']}{m['position']}" for m in b["mapping"] if m["design"]])
    if task.get("memory_tier", 0) >= 1:
        settings.update(batch_size=1, number_of_batches=c["mpnn"]["sequences"])
    write_json(directory / "mpnn_input.json", settings)
    output = engine.run(input_dicts=[settings])
    if len(output) != c["mpnn"]["sequences"]:
        raise ValueError("Unexpected MPNN sample count")
    records = []
    reference = Protein(c["input"])
    for index, item in enumerate(output):
        path = directory / f"s{index:04d}.cif"
        to_cif_file(item.atom_array, str(path))
        protein = Protein(path)
        require_order(protein, b["mapping"])
        seq = "".join(r.amino_acid for r in protein.records)
        validate_sequence(seq, b["mapping"], reference)
        # MPNN is fixed-backbone, including design sites.
        original = Protein(backbone_path(plan, task))
        backbone_atoms = lambda p: np.array([r[a].coord for r in p.residues for a in ("N", "CA", "C", "O")])
        if not np.allclose(backbone_atoms(protein), backbone_atoms(original), atol=0.005, rtol=0):
            raise ValueError("MPNN altered backbone coordinates")
        chains = {}
        for aa, mapping in zip(seq, b["mapping"]):
            chains[mapping["chain"]] = chains.get(mapping["chain"], "") + aa
        name = f"{b['id']}_s{index:04d}"
        (directory / f"s{index:04d}.fasta").write_text(f">{name}\n{':'.join(chains.values())}\n")
        from .contracts import candidate_record
        records.append(candidate_record(plan, b["id"], index, chains, b["seed"]))
    write_json(directory / "sequences.json", records)


def sequence_record(plan, task):
    if plan.get("gpu_capsule"):
        return plan["gpu_capsule"]["record"]
    if plan.get("mode") == "folding":
        return plan["fold_records"][task["record"]]
    b = plan["backbones"][task["backbone"]]
    return read_json(Path(plan["config"]["output"]) / "tasks" / f"mpnn_{b['id']}" / "sequences.json")[task["sequence"]]


def a3m_query(path):
    lines = Path(path).read_text().splitlines()
    seq, active = [], False
    for line in lines:
        if line.startswith(">"):
            if active:
                break
            active = True
        elif active and line and not line.startswith("#"):
            seq.append(line.strip())
    return "".join(seq)


def msa(plan, task, directory):
    if ("members" in task or plan["config"]["msa"].get("optimization", {}).get("enabled")
            or any(plan["config"]["msa"].get(k) for k in ("gpu", "gpu_server", "ignore_index", "db_load_mode"))):
        from .msa_batch import run
        return run(plan, task, directory)
    c = plan["config"]["msa"]
    record = sequence_record(plan, task)
    script = Path(c["script"])
    if not c["database_version"]:
        raise ValueError("MSA database_version must identify the installed database release")
    import inspect
    env = {k: v for k, v in plan["environment"]["msa"].items() if k != "code"}
    key = fingerprint({"chains": list(record["chains"].values()), "settings": {k: v for k, v in c.items() if k != "cache_root"},
                       "script": digest(script), "versions": env,
                       "adapter": inspect.getsource(msa) + inspect.getsource(a3m_query)})
    cache = Path(c.get("cache_root") or Path(plan["config"]["output"]) / "msa_cache") / key
    cache.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(cache / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = cache / "complete.json"
        valid = marker.exists() and all(Path(p).is_file() and digest(p) == h for p, h in read_json(marker).items())
        if not valid:
            import shutil
            results = cache / "search"
            if results.exists():
                shutil.rmtree(results)
            query = cache / "query.fasta"
            query.write_text(f">query\n{':'.join(record['chains'].values())}\n")
            # Invoke search directly to propagate failures; use upstream only for conversion.
            subprocess.run([str(Path(sys.executable).parent / "colabfold_search"), str(query), c["database"], str(results),
                "--db1", c["db1"], "--db3", c["db3"], "--mmseqs", c["mmseqs"],
                "--threads", str(c["threads"]), "--gpu", "0"], check=True)
            source = list(results.glob("*.a3m"))
            if len(source) != 1:
                raise ValueError("Expected one multimer-aware ColabFold A3M")
            spec = importlib.util.spec_from_file_location("colabfold_converter", script)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            processor = module.A3MProcessor(str(source[0]), str(cache))
            if len(processor.chain_info) == 2:
                processor.split_sequences()
            files = sorted((cache / "msa").rglob("*.a3m"))
            if not files:
                raise ValueError("ColabFold conversion produced no MSAs")
            write_json(marker, {str(p): digest(p) for p in files})
        # ColabFold deduplicates identical chains, retaining first-occurrence order.
        unique = list(dict.fromkeys(record["chains"].values()))
        inputs = []
        for chain, seq in record["chains"].items():
            base = cache / "msa" / str(unique.index(seq))
            paired, unpaired = base / "pairing.a3m", base / "non_pairing.a3m"
            for path in (paired, unpaired):
                if a3m_query(path) != seq:
                    raise ValueError(f"MSA query differs from designed sequence: {path}")
            inputs.append({"proteinChain": {"sequence": seq, "count": 1, "id": [chain],
                "pairedMsaPath": str(paired), "unpairedMsaPath": str(unpaired)}})
        write_json(directory / "fold_input.json", [{"name": record["id"], "sequences": inputs}])
        write_json(directory / "msa_artifacts.json", read_json(marker))


def fold(plan, task, directory):
    c = plan["config"]
    record = sequence_record(plan, task)
    msa_id = (task.get("deps") or [f"msa_{record['id']}"])[0]
    msa_task = plan.get("tasks", {}).get(msa_id, {})
    filename = Path("fold_inputs") / (record["id"] + ".json") if "members" in msa_task else Path("fold_input.json")
    source = Path(c["output"]) / "tasks" / msa_id / filename
    if plan.get("gpu_capsule"):
        source = Path(c["output"]) / "fold_input.json"
    options = c["folding"]["options"]
    allowed = {"cycle", "step", "dtype", "trimul_kernel", "triatt_kernel", "chunk_size", "diffusion_chunk_size"}
    if set(options) - allowed:
        raise ValueError(f"Unsupported folding options: {set(options) - allowed}")
    checkpoint = Path(c["folding"]["weights"]) / "protenix-v2.pt"
    if not checkpoint.is_file():
        raise ValueError(f"Required v2 checkpoint missing: {checkpoint}")
    args = [sys.executable, "-m", "runner.inference", "--input_json_path", str(source), "--dump_dir", str(directory / "raw"),
            "--model_name", "protenix-v2", "--seeds", str(task["seed"]),
            "--sample_diffusion.N_sample", str(c["folding"]["samples"]), "--use_msa", "true",
            "--load_checkpoint_dir", c["folding"]["weights"]]
    names = {"cycle": "model.N_cycle", "step": "sample_diffusion.N_step", "dtype": "dtype",
             "trimul_kernel": "triangle_multiplicative", "triatt_kernel": "triangle_attention",
             "chunk_size": "infer_setting.chunk_size", "diffusion_chunk_size": "infer_setting.sample_diffusion_chunk_size"}
    if c["folding"].get("need_atom_confidence"):
        args += ["--need_atom_confidence", "true"]
    for key, value in options.items():
        args += ["--" + names[key], str(value)]
    write_json(directory / "command.json", args)
    subprocess.run(args, check=True)
    paths = sorted((directory / "raw").rglob("*.cif"))
    if len(paths) != c["folding"]["samples"]:
        raise ValueError(f"Protenix produced {len(paths)} predictions; expected {c['folding']['samples']}")
    predictions = []
    import re
    for path in paths:
        match = re.search(r"sample[_-]?(\d+)", path.stem)
        if not match:
            raise ValueError(f"Unrecognized Protenix sample filename: {path.name}")
        sample = int(match[1])
        if plan.get("mode") == "folding":
            from .folding_results import validate_structure
            validate_structure(path, record["job"])
        else:
            protein = Protein(path)
            # Explicit entity IDs in the input preserve sequence-to-chain correspondence.
            chains = {}
            for r in protein.records:
                chains[r.chain] = chains.get(r.chain, "") + r.amino_acid
            if chains != record["chains"]:
                raise ValueError("Folding output chain IDs/sequences do not match the input")
        confidence = list(path.parent.glob(f"*summary_confidence*sample_{sample}.json"))
        if len(confidence) != 1:
            raise ValueError(f"Missing or ambiguous confidence for {path}")
        predictions.append({"path": str(path), "sample": sample, "confidence": str(confidence[0])})
    if sorted(p["sample"] for p in predictions) != list(range(c["folding"]["samples"])):
        raise ValueError("Prediction sample indices are missing or duplicated")
    write_json(directory / "predictions.json", predictions)


def metrics(plan, task, directory):
    if plan.get("mode") == "folding":
        from .folding_results import metrics as folding_metrics
        return folding_metrics(plan, task, directory)
    c, b = plan["config"], plan["backbones"][task["backbone"]]
    record = sequence_record(plan, task)
    predictions = read_json(Path(c["output"]) / "tasks" / task["deps"][0] / "predictions.json")
    rows = []
    for item in predictions:
        pred = Protein(item["path"])
        # Order by explicit expected IDs rather than parser traversal order.
        lookup = {(r.chain, r.position): xyz for r, xyz in zip(pred.records, pred.ca())}
        coords = np.array([lookup[(m["chain"], m["position"])] for m in b["mapping"]])
        row = {"design": record["id"], "backbone": b["id"], "design_seed": b["seed"], "fold_seed": task["seed"],
               "sample": item["sample"], "length": len(coords), "status": "succeeded", "prediction": item["path"],
               "sequence": record["sequence"], "confidence_path": item["confidence"]}
        from .contracts import evaluation_identity
        row.update(evaluation_identity(plan, record, task, item["sample"]))
        row["case"] = b.get("case", "default")
        original = compare(Protein(c["input"]).ca(), coords, [m["reference"] for m in b["mapping"]], set(b.get("selected", plan["selected"])))
        row.update({"reference_" + k: v for k, v in original.items()})
        if c["rfd3"]["enabled"]:
            backbone = compare(Protein(backbone_path(plan, task)).ca(), coords, list(range(len(coords))),
                               {i for i, m in enumerate(b["mapping"]) if m["design"]})
            row.update({"backbone_" + k: v for k, v in backbone.items()})
        confidence = read_json(item["confidence"])
        if isinstance(confidence, list) and len(confidence) == 1:
            confidence = confidence[0]
        row.update({"confidence_" + k: v for k, v in confidence.items() if isinstance(v, (str, int, float))})
        rows.append(row)
    write_json(directory / "metrics.json", rows)
