"""P2 computational validity report; no biological efficacy or ranking claims."""
import csv
from collections import Counter
from pathlib import Path
from .io import write_json


def report(root):
    root = Path(root)
    with open(root / "summary.csv", newline="") as stream:
        rows = list(csv.DictReader(stream))
    results = []
    for row in rows:
        reasons = []
        if row["status"] != "succeeded":
            reasons.append("pipeline_" + row["status"])
        for key in ("reference_all_coverage", "reference_retained_coverage"):
            try:
                if float(row.get(key, "")) != 1:
                    reasons.append("incomplete_" + key)
            except ValueError:
                reasons.append("missing_" + key)
        results.append({"candidate_id": row.get("candidate_id"), "prediction_id": row.get("prediction_id"),
                        "design": row.get("design"), "sample": row.get("sample"),
                        "decision": "computationally_evaluable" if not reasons else "insufficient_evidence",
                        "reasons": reasons})
    payload = {"schema_version": 1, "policy": "p2-completeness-v1", "results": results,
               "counts": dict(Counter(x["decision"] for x in results)),
               "scope": "Completeness/coverage only; not structural quality selection or experimental efficacy."}
    write_json(root / "evaluation_report.json", payload)
    return payload
