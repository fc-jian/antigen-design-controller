import csv
from pathlib import Path
import pytest
from antigen_design.io import write_json, digest, read_json
from antigen_design.pipeline import initialize, advance, refresh, summarize, verify_plan
from antigen_design.scheduler import slurm_script, Slurm
from antigen_design.worker import receipt_valid


class Scheduler:
    def __init__(self):
        self.submitted = []
        self.states = {}

    def submit(self, task, script):
        job = str(len(self.submitted) + 1)
        self.submitted.append((task, script))
        self.states[job] = "RUNNING"
        return job

    def state(self, task):
        return self.states.get(task["job_id"], "UNKNOWN")


def receipt(plan, name):
    directory = Path(plan["config"]["output"]) / "tasks" / name
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "result.txt"
    output.write_text("validated test artifact")
    write_json(directory / "complete.json", {"signature": plan["tasks"][name]["signature"], "artifacts": {str(output): digest(output)}})


def make_plan(config):
    config["execution"]["backend"] = "slurm"
    plan = initialize(config, dry_run=True)
    return plan, read_json(Path(config["output"]) / "manifest.json")


def test_cross_cluster_dependency_and_missing_artifact(config):
    plan, state = make_plan(config)
    scheduler = Scheduler()
    assert not advance(plan, state, scheduler)
    assert [t[0]["stage"] for t in scheduler.submitted] == ["mpnn"]
    task = scheduler.submitted[0][0]
    assert task["host"] == "cluster-gpu"
    scheduler.states["1"] = "COMPLETED"
    # Slurm success alone cannot release dependencies.
    advance(plan, state, scheduler)
    assert state["tasks"][task["id"]]["state"] == "failed"
    assert len(scheduler.submitted) == 1
    advance(plan, state, scheduler, retry=True)
    receipt(plan, task["id"])
    scheduler.states["2"] = "COMPLETED"
    advance(plan, state, scheduler)
    assert [t[0]["stage"] for t in scheduler.submitted] == ["mpnn", "mpnn", "msa", "msa"]
    assert all(t[0]["host"] == "cluster-cpu" for t in scheduler.submitted[2:])
    assert all("afterok" not in path.read_text() for _, path in scheduler.submitted)


def test_resume_no_duplicate_and_checksum_invalidation(config):
    plan, state = make_plan(config)
    scheduler = Scheduler()
    advance(plan, state, scheduler)
    advance(plan, state, scheduler)
    assert len(scheduler.submitted) == 1
    name = scheduler.submitted[0][0]["id"]
    receipt(plan, name)
    scheduler.states["1"] = "COMPLETED"
    advance(plan, state, scheduler)
    output = Path(config["output"]) / "tasks" / name / "result.txt"
    output.write_text("corrupted")
    assert not receipt_valid(output.parent)
    refresh(plan, state, scheduler)
    assert state["tasks"][name]["state"] == "failed"


def test_failure_rows_and_submitting_recovery(config):
    plan, state = make_plan(config)
    scheduler = Scheduler()
    advance(plan, state, scheduler)
    scheduler.states["1"] = "OUT_OF_MEMORY"
    assert advance(plan, state, scheduler)
    rows = list(csv.DictReader(open(Path(config["output"]) / "summary.csv")))
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"blocked"}
    first = next(iter(state["tasks"].values()))
    first.update(state="submitting")
    first.pop("job_id")
    with pytest.raises(RuntimeError, match="outcome unknown"):
        refresh(plan, state, scheduler)


def test_dry_run_cannot_execute_or_overwrite(config):
    plan = initialize(config, dry_run=True)
    with pytest.raises(ValueError, match="Dry-run"):
        verify_plan(plan)
    with pytest.raises(ValueError, match="not empty"):
        initialize(config, dry_run=True)


def test_script_escaping_and_validation(tmp_path):
    text = slurm_script(["/env path/bin/python", "a;echo hacked"], tmp_path / "a b", tmp_path / "log", {"cpus": 4, "partition": "gpu"}, "job")
    assert "'a;echo hacked'" in text
    assert "#SBATCH --cpus-per-task=4" in text
    with pytest.raises(ValueError):
        slurm_script(["python"], tmp_path, tmp_path / "log", {"partition": "gpu\ncommand"}, "job")


def test_sacct_nonzero_exit(monkeypatch):
    monkeypatch.setattr("antigen_design.scheduler.command", lambda *a, **k: "42|COMPLETED|1:0|")
    assert Slurm().state({"job_id": "42", "host": "cluster-gpu"}) == "FAILED"
