"""Versioned records; candidate origin and sequence content have separate identities."""
from .io import fingerprint

SCHEMA_VERSION = 1
METRIC_VERSION = "ca-mapped-v1"


def candidate_record(plan, backbone, index, chains, seed):
    if not chains or any(not seq or set(seq) - set("ACDEFGHIKLMNPQRSTVWY") for seq in chains.values()):
        raise ValueError("Candidate needs nonempty canonical protein chains")
    sequence_id = fingerprint(list(chains.values()))
    origin = plan.get("gpu_capsule", {}).get("origin_identity", plan["identity"])
    cid = fingerprint({"run": origin, "backbone": backbone, "sample": index})
    return {"schema_version": SCHEMA_VERSION, "candidate_id": cid,
            "sequence_id": sequence_id, "id": f"{backbone}_s{index:04d}",
            "parent_backbone": backbone, "chains": chains, "sequence": "".join(chains.values()),
            "seed": seed, "run_id": origin, "model": "foundry-proteinmpnn"}


def evaluation_identity(plan, record, task, sample):
    return {"schema_version": SCHEMA_VERSION, "metric_version": METRIC_VERSION,
            "candidate_id": record["candidate_id"], "sequence_id": record["sequence_id"],
            "prediction_id": fingerprint({"candidate": record["candidate_id"], "task": task["id"], "sample": sample}),
            "prediction_model": plan["config"]["folding"]["model"], "run_id": plan["identity"]}
