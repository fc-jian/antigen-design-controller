from pathlib import Path
import pytest
from antigen_design.io import read_json, write_json
from test_pipeline import make_plan


def test_boundary_requires_completed_msa_and_no_fold_submission(config, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_acceptance import boundary
    plan, manifest = make_plan(config)
    root = Path(config["output"])
    evidence = tmp_path / "evidence"
    with pytest.raises(ValueError, match="incomplete"):
        boundary(root, evidence)
    for name, task in plan["tasks"].items():
        if task["stage"] in ("mpnn", "msa"):
            manifest["tasks"][name]["state"] = "succeeded"
    write_json(root / "manifest.json", manifest)
    boundary(root, evidence)
    saved = read_json(evidence / "msa-boundary.json")
    assert all(not saved["tasks"][name].get("job_id") for name, task in plan["tasks"].items() if task["stage"] == "fold")
    first = next(n for n, t in plan["tasks"].items() if t["stage"] == "fold")
    manifest["tasks"][first]["job_id"] = "123"
    write_json(root / "manifest.json", manifest)
    with pytest.raises(ValueError, match="not to have been submitted"):
        boundary(root, evidence)


@pytest.mark.parametrize("state,error,expected_calls", [
    ("submitted", "Connection timed out", 2),
    ("submitting", "Connection timed out", 1),
    ("failed", "Connection timed out", 1),
    ("submitted", "permanent model/environment error", 1),
])
def test_acceptance_reconnect_is_bounded_and_does_not_retry_jobs(tmp_path, monkeypatch, state, error, expected_calls):
    import io
    import subprocess
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_acceptance
    write_json(tmp_path / "manifest.json", {"tasks": {"msa": {"state": state, "job_id": "known-job"}}})
    calls = []
    class Process:
        def __init__(self, *a, **kw):
            calls.append(a)
            self.code = 1 if len(calls) == 1 else 0
            self.stdout = io.StringIO(error + "\n" if self.code else "success\n")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def wait(self): return self.code
    monkeypatch.setattr(run_acceptance.subprocess, "Popen", Process)
    monkeypatch.setattr(run_acceptance.time, "sleep", lambda _: None)
    if expected_calls == 2:
        run_acceptance.run_command(["python", "-m", "cli", "resume"], tmp_path, tmp_path)
        assert read_json(tmp_path / "transport-retries.json")[0]["existing_jobs"] == {"msa": "known-job"}
    else:
        with pytest.raises(subprocess.CalledProcessError):
            run_acceptance.run_command(["python", "-m", "cli", "resume"], tmp_path, tmp_path)
    assert len(calls) == expected_calls


def test_acceptance_reconnect_stops_at_limit(tmp_path, monkeypatch):
    import io
    import subprocess
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_acceptance
    write_json(tmp_path / "manifest.json", {"tasks": {"msa": {"state": "submitted", "job_id": "one-job"}}})
    calls = []
    class Process:
        def __init__(self, *a, **kw):
            calls.append(a)
            self.stdout = io.StringIO("Connection timed out\n")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def wait(self): return 1
    monkeypatch.setattr(run_acceptance.subprocess, "Popen", Process)
    monkeypatch.setattr(run_acceptance.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError):
        run_acceptance.run_command(["python", "-m", "cli", "resume"], tmp_path, tmp_path, retries=2)
    assert len(calls) == 3
    assert len(read_json(tmp_path / "transport-retries.json")) == 2
