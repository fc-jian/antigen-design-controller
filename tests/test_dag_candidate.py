"""Candidate scheduler regression tests; no cluster jobs or model inference."""
from copy import deepcopy
import pytest
from antigen_design.config import DEFAULTS
from antigen_design.pipeline import task_graph
from antigen_design import remote, pipeline, gpu_routing


pytestmark = pytest.mark.skipif("batch_scope" not in DEFAULTS["msa"],
                              reason="Requires isolated DAG candidate source")


def test_batches_do_not_wait_for_unrelated_backbones():
    config = deepcopy(DEFAULTS)
    config["msa"].update(batch_size=16, batch_scope="backbone")
    config["mpnn"]["sequences"] = 20
    plan = {"config": config, "identity": "test", "backbones": [{"id": "a"}, {"id": "b"}]}
    tasks = task_graph(plan)
    batches = [t for t in tasks.values() if t["stage"] == "msa"]
    assert [len(t["members"]) for t in batches] == [16, 4, 16, 4]
    assert [t["deps"] for t in batches] == [["mpnn_a"], ["mpnn_a"], ["mpnn_b"], ["mpnn_b"]]
    folds = [t for t in tasks.values() if t["stage"] == "fold"]
    assert len(folds) == 40
    assert all(len(t["deps"]) == 1 for t in folds)


def test_ready_branches_submit_together_and_respect_limits(tmp_path, monkeypatch):
    config = deepcopy(DEFAULTS)
    config["execution"].update(max_gpu_jobs=2, max_msa_jobs=2)
    tasks = {"up": {"stage": "mpnn", "deps": []},
             "slow": {"stage": "mpnn", "deps": []},
             "bad": {"stage": "mpnn", "deps": []},
             "msa_a": {"stage": "msa", "deps": ["up"]},
             "msa_b": {"stage": "msa", "deps": ["slow"]},
             "msa_bad": {"stage": "msa", "deps": ["bad"]},
             "fold_a": {"stage": "fold", "deps": ["up"]},
             "fold_b": {"stage": "fold", "deps": ["up"]},
             "metrics": {"stage": "metrics", "deps": ["up"]}}
    states = {n: {"stage": t["stage"], "state": "pending", "attempts": 0,
                  "host": "test", "memory_tier": 0} for n, t in tasks.items()}
    states["up"]["state"] = "succeeded"
    states["slow"]["state"] = "running"
    states["bad"]["state"] = "failed"
    plan = {"config": config, "tasks": tasks, "local_output": str(tmp_path)}
    manifest = {"tasks": states}
    submitted = []
    monkeypatch.setattr(remote, "refresh", lambda *a: None)
    monkeypatch.setattr(remote, "check_prerequisites", lambda *a: None)
    monkeypatch.setattr(pipeline, "summarize", lambda *a: None)
    monkeypatch.setattr(remote, "boundary_complete", lambda *a: False)
    def submit(*args):
        submitted.append(args[-1]["task"])
        return {"job_id": str(len(submitted))}
    monkeypatch.setattr(remote, "control", submit)
    remote.advance(plan, manifest, None)
    assert submitted == ["metrics", "msa_a", "fold_a"]
    assert states["msa_b"]["state"] == "pending"
    assert states["msa_bad"]["state"] == "blocked"
    assert states["fold_b"]["state"] == "pending"
    states["slow"]["state"] = "succeeded"
    remote.advance(plan, manifest, None)
    assert submitted[-2:] == ["msa_b", "fold_b"]


def test_pending_submission_reserves_idle_capacity(monkeypatch):
    config = deepcopy(DEFAULTS)
    target = {"id": "t", "resources": {"gres": "gpu:1", "cpus": 4, "memory": "32G"}}
    config["execution"]["gpu_routing"]["targets"] = [target]
    plan = {"config": config}
    observation = {"id": "t", "free_gpus": 1,
                   "nodes": [{"gpus": 1, "cpus": 4, "memory_mb": 32768}]}
    manifest = {"gpu_routing": {}, "tasks": {
        "one": {"state": "submitted", "route": {"target": target}}}}
    def snapshot(*args, **kwargs):
        assert kwargs["force"] is True
        return [observation]
    monkeypatch.setattr(gpu_routing, "snapshots", snapshot)
    monkeypatch.setattr(gpu_routing, "ready", lambda *a: {"validated": True})
    assert gpu_routing.choose(plan, manifest, None) is None
    manifest["tasks"]["one"]["state"] = "succeeded"
    assert gpu_routing.choose(plan, manifest, None) == (target, {"validated": True})
