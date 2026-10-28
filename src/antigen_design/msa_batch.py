"""Batched ColabFold searches with per-sequence caches and explicit execution mode."""
from contextlib import ExitStack
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from .io import read_json, write_json, digest, fingerprint


def cache_valid(directory):
    try:
        artifacts = read_json(directory / "complete.json")
        return isinstance(artifacts, dict) and bool(artifacts) and all(Path(p).is_relative_to(directory) and Path(p).is_file()
                                       and digest(p) == h for p, h in artifacts.items())
    except (OSError, ValueError, TypeError):
        return False


def fold_input(record, cache):
    from .adapters import a3m_query
    unique = list(dict.fromkeys(record["chains"].values()))
    inputs = []
    for chain, seq in record["chains"].items():
        base = cache / "msa" / str(unique.index(seq))
        paired, unpaired = base / "pairing.a3m", base / "non_pairing.a3m"
        if any(a3m_query(p) != seq for p in (paired, unpaired)):
            raise ValueError("MSA query differs from candidate chain")
        inputs.append({"proteinChain": {"sequence": seq, "count": 1, "id": [chain],
            "pairedMsaPath": str(paired), "unpairedMsaPath": str(unpaired)}})
    return [{"name": record["id"], "sequences": inputs}]


def run(plan, task, directory):
    from .adapters import sequence_record
    started = time.monotonic()
    c = plan["config"]["msa"]
    all_records = [sequence_record(plan, m) for m in task.get("members", [task])]
    records = [r for r in all_records if r["chains"]]
    if not c["database_version"]:
        raise ValueError("MSA database_version is required")
    environment = {k: v for k, v in plan["environment"]["msa"].items() if k != "code"}
    settings = {k: v for k, v in c.items() if k not in ("cache_root", "batch_size")}
    identity = {"schema": "colabfold-batch-v1", "settings": settings, "environment": environment,
                "converter": digest(c["script"]), "adapter": digest(__file__)}
    cache_root = Path(c.get("cache_root") or Path(plan["config"]["output"]) / "msa_cache").resolve()
    keys = {r["id"]: fingerprint(identity | {"chains": list(r["chains"].values())}) for r in records}
    unique = {keys[r["id"]]: r for r in records}
    artifacts, hits, misses = {}, [], []
    search_invocations = 0
    with ExitStack() as stack:
        # Global ordering avoids deadlock between partially overlapping concurrent batches.
        for key in sorted(unique):
            cache = cache_root / key
            cache.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(open(cache / ".lock", "w"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            (hits if cache_valid(cache) else misses).append(key)
        # Lock order remains globally sorted, but query order must not depend on
        # host-specific cache identities. Preserve first occurrence in input.
        query_order = {key: i for i, key in enumerate(dict.fromkeys(keys.values()))}
        misses.sort(key=query_order.__getitem__)
        if misses:
            query = directory / "queries.fasta"
            names = {key: f"q{i:06d}" for i, key in enumerate(misses)}
            query.write_text("".join(f">{names[k]}\n{':'.join(unique[k]['chains'].values())}\n" for k in misses))
            results = directory / "search"
            args = [str(Path(sys.executable).parent / "colabfold_search"), str(query), c["database"], str(results),
                    "--db1", c["db1"], "--db3", c["db3"], "--mmseqs", c["mmseqs"],
                    "--threads", str(c["threads"]), "--db-load-mode", str(c["db_load_mode"]),
                    "--gpu-server", str(int(c["gpu_server"])), "--gpu", str(int(c["gpu"]))]
            env = os.environ.copy()
            env.pop("MMSEQS_IGNORE_INDEX", None)
            if c["ignore_index"]:
                env["MMSEQS_IGNORE_INDEX"] = "1"
            write_json(directory / "search_command.json", {"argv": args, "ignore_index": c["ignore_index"],
                                                          "cuda_visible_devices": env.get("CUDA_VISIBLE_DEVICES")})
            if c.get("optimization", {}).get("enabled"):
                from .msa_execution import search
                search_invocations = search(c, args, env, directory)
            else:
                subprocess.run(args, check=True, env=env)
                search_invocations = 1
            outputs = set(p.name for p in results.glob("*.a3m"))
            if outputs != {name + ".a3m" for name in names.values()}:
                raise ValueError("Batch search output accounting mismatch")
            spec = importlib.util.spec_from_file_location("colabfold_converter", c["script"])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            for key in misses:
                cache = cache_root / key
                (cache / "complete.json").unlink(missing_ok=True)
                if (cache / "msa").exists():
                    shutil.rmtree(cache / "msa")
                processor = module.A3MProcessor(str(results / (names[key] + ".a3m")), str(cache))
                if len(processor.chain_info) == 2:
                    processor.split_sequences()
                fold_input(unique[key], cache)  # validate before publishing the cache receipt
                files = {str(p): digest(p) for p in (cache / "msa").rglob("*.a3m") if p.stat().st_size}
                if not files:
                    raise ValueError("Empty converted MSA")
                write_json(cache / "complete.json", files)
        for record in all_records:
            cache = cache_root / keys[record["id"]] if record["chains"] else None
            payload = fold_input(record, cache) if cache else [{"name": record["id"], "sequences": []}]
            if "job" in record:
                from copy import deepcopy
                job = record["job"]
                msa_by_chain = {e["proteinChain"]["id"][0]: e["proteinChain"] for e in payload[0]["sequences"]}
                entities = deepcopy(job["sequences"])
                for entity in entities:
                    if "proteinChain" in entity:
                        protein = entity["proteinChain"]
                        msa = msa_by_chain[protein["id"][0]]
                        protein.update({key: msa[key] for key in ("pairedMsaPath", "unpairedMsaPath")})
                payload = [{"name": job["name"], "sequences": entities, "covalent_bonds": job["covalent_bonds"]}]
            output = directory / "fold_inputs" / (record["id"] + ".json") if "members" in task else directory / "fold_input.json"
            write_json(output, payload)
            if cache:
                artifacts.update(read_json(cache / "complete.json"))
        write_json(directory / "msa_artifacts.json", artifacts)
        write_json(directory / "msa_batch.json", {"schema_version": 1, "candidates": len(records),
            "unique_sequences": len(unique), "cache_hits": len(hits), "searched_sequences": len(misses),
            "search_invocations": search_invocations, "elapsed_seconds": time.monotonic() - started,
            "candidate_cache_keys": keys, "gpu": c["gpu"], "gpu_server": c["gpu_server"],
            "db_load_mode": c["db_load_mode"], "ignore_index": c["ignore_index"]})
