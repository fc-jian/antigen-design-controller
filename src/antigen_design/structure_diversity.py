"""Conservative structural diversity within identical residue-map topologies."""
from pathlib import Path
import tarfile
import tempfile
import numpy as np
from .io import read_json, digest
from .structure import Protein
from .metrics import fit


def distances(root, plan, rows):
    root = Path(root)
    remote_root = Path(plan["config"]["output"])
    bundle = root / "deliverables.tar.gz"
    archive = None
    if bundle.exists():
        if digest(bundle) != read_json(root / "download_verification.json")["sha256"]:
            raise ValueError("Structural diversity archive checksum mismatch")
        archive = tarfile.open(bundle, "r:gz")
    candidates, topologies, source_hashes = {}, {}, {}
    try:
        for row in rows:
            if row.get("status") != "succeeded":
                continue
            cid = row["candidate_id"]
            b = next(b for b in plan["backbones"] if b["id"] == row["backbone"])
            mapping = b["mapping"]
            topologies[cid] = tuple((m["chain"], m["position"], m["reference"], m["design"]) for m in mapping)
            path = Path(row["prediction"])
            if not path.is_relative_to(remote_root):
                raise ValueError("Prediction path outside run")
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / path.name
                if archive:
                    member = archive.getmember(path.relative_to(remote_root).as_posix())
                    if not member.isfile() or member.size > 20 * 1024 * 1024:
                        raise ValueError("Invalid structure archive member")
                    local.write_bytes(archive.extractfile(member).read())
                else:
                    local.write_bytes(path.read_bytes())
                protein = Protein(local)
                source_hashes[row["prediction_id"]] = digest(local)
                lookup = {(r.chain, r.position): xyz for r, xyz in zip(protein.records, protein.ca())}
                coords = np.array([lookup[(m["chain"], m["position"])] for m in mapping])
                candidates.setdefault(cid, []).append(coords)
    finally:
        if archive:
            archive.close()
    result = {}
    for a in sorted(candidates):
        for b in sorted(candidates):
            if b <= a:
                continue
            if topologies[a] != topologies[b]:
                result[(a, b)] = None
                continue
            values = []
            for x in candidates[a]:
                for y in candidates[b]:
                    transform = fit(x, y)
                    if transform is None:
                        raise ValueError("Insufficient points for structural diversity")
                    values.append(float(np.sqrt(np.mean(np.sum((x @ transform[0] + transform[1] - y) ** 2, axis=1)))))
            result[(a, b)] = max(values)
    def distance(a, b):
        return result[tuple(sorted((a, b)))]
    return distance, {"metric": "maximum cross-ensemble aligned CA RMSD; identical residue maps only",
                      "prediction_sha256": source_hashes,
                      "pairs": [{"a": a, "b": b, "rmsd": v, "reason": "" if v is not None else "incomparable_residue_maps"}
                                for (a, b), v in result.items()]}
