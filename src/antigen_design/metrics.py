import numpy as np


def fit(mobile, target):
    mobile, target = np.asarray(mobile, float), np.asarray(target, float)
    if len(mobile) < 3 or np.linalg.matrix_rank(mobile - mobile.mean(0)) < 2 or np.linalg.matrix_rank(target - target.mean(0)) < 2:
        return None
    mc, tc = mobile.mean(0), target.mean(0)
    u, _, vt = np.linalg.svd((mobile - mc).T @ (target - tc))
    sign = np.diag([1, 1, np.linalg.det(u @ vt)])
    rotation = u @ sign @ vt
    return rotation, tc - mc @ rotation


def compare(reference, prediction, mapping, reference_design):
    """Mapping is prediction index -> reference index or None. lDDT averages residue scores."""
    reference, prediction = np.asarray(reference), np.asarray(prediction)
    if reference.ndim != 2 or prediction.ndim != 2 or reference.shape[1:] != (3,) or prediction.shape[1:] != (3,):
        raise ValueError("Coordinates must have shape (N,3)")
    if not np.isfinite(reference).all() or not np.isfinite(prediction).all():
        raise ValueError("Coordinates must be finite")
    if any(j is not None and (not isinstance(j, (int, np.integer)) or isinstance(j, bool) or not 0 <= j < len(reference)) for j in mapping):
        raise ValueError("Mapping index outside reference")
    if any(not 0 <= j < len(reference) for j in reference_design):
        raise ValueError("Design index outside reference")
    if len(prediction) != len(mapping):
        raise ValueError("Prediction length does not match mapping")
    valid = [(i, j) for i, j in enumerate(mapping) if j is not None]
    if len({j for _, j in valid}) != len(valid):
        raise ValueError("Mapping is not one-to-one")
    pi = np.array([i for i, _ in valid], dtype=int)
    ri = np.array([j for _, j in valid], dtype=int)
    x, y = prediction[pi], reference[ri]
    design = np.array([j in reference_design for j in ri], dtype=bool)
    masks = {"all": np.ones(len(ri), dtype=bool), "design": design, "retained": ~design}
    rd = np.linalg.norm(y[:, None] - y[None, :], axis=-1)
    pd = np.linalg.norm(x[:, None] - x[None, :], axis=-1)
    neighbors = (rd < 15) & ~np.eye(len(ri), dtype=bool)
    score = sum((np.abs(rd - pd) < t).astype(float) for t in (0.5, 1, 2, 4)) / 4
    counts = neighbors.sum(1)
    per_residue = np.divide((score * neighbors).sum(1), counts, out=np.full(len(ri), np.nan), where=counts > 0)
    result = {}
    for region, mask in masks.items():
        expected = len(reference) if region == "all" else len(reference_design) if region == "design" else len(reference) - len(reference_design)
        n = int(mask.sum())
        result[f"{region}_mapped"] = n
        result[f"{region}_coverage"] = n / expected if expected else None
        good = per_residue[mask & np.isfinite(per_residue)]
        result[f"{region}_lddt"] = float(good.mean()) if len(good) else None
        result[f"{region}_lddt_reason"] = "" if len(good) else "no_mapped_residues_or_neighbors"
    for alignment in ("all", "retained"):
        mask = masks[alignment]
        transform = fit(x[mask], y[mask])
        errors = np.sum((x @ transform[0] + transform[1] - y)**2, axis=1) if transform else None
        for region, mask in masks.items():
            key = f"{region}_rmsd_fit_{alignment}"
            result[key] = float(np.sqrt(errors[mask].mean())) if transform and mask.any() else None
            result[key + "_reason"] = "" if result[key] is not None else "insufficient_alignment_points" if not transform else "no_mapped_residues"
    return result


def validate_fixed(reference, generated, mapping, tolerance=0.05):
    if len(generated.records) != len(mapping):
        raise ValueError("Generated backbone length differs from planned mapping")
    x, y = [], []
    for residue, item in zip(generated.residues, mapping):
        if item["design"]:
            continue
        old = reference.residues[item["reference"]]
        if residue.resname != old.resname:
            raise ValueError("RFD3 changed a retained residue sequence")
        for atom in ("N", "CA", "C", "O"):
            x.append(residue[atom].coord)
            y.append(old[atom].coord)
    if not x:
        return
    transform = fit(x, y)
    if transform is None:
        raise ValueError("Insufficient fixed backbone geometry to validate constraints")
    deviation = np.linalg.norm(np.asarray(x) @ transform[0] + transform[1] - np.asarray(y), axis=1)
    if deviation.max() > tolerance:
        raise ValueError(f"Retained backbone moved: maximum aligned deviation {deviation.max():.3f} Å")
