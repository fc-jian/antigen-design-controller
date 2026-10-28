from copy import deepcopy
import fcntl
import pytest
from antigen_design.config import DEFAULTS
from antigen_design.io import write_json, read_json
from antigen_design.runtime_limits import admission, validate, DEFAULT_LIMITS
from antigen_design.web_store import Store


@pytest.mark.parametrize("value", [
    {"max_gpu_jobs": -1, "max_msa_jobs": 8},
    {"max_gpu_jobs": True, "max_msa_jobs": 8},
    {"max_gpu_jobs": 0, "max_msa_jobs": 0},
    {"max_gpu_jobs": 0, "max_msa_jobs": 1.5},
    {"max_gpu_jobs": 0},
])
def test_invalid_policy_rejected(value):
    with pytest.raises(ValueError):
        validate(value)


def test_policy_is_live_shared_and_does_not_change_frozen_config(tmp_path):
    store = Store(tmp_path)
    assert store.scheduler_limits() == DEFAULT_LIMITS
    own, other = "a" * 32, "b" * 32
    write_json(store.registry, [own, other])
    for job in (own, other):
        write_json(store.root / "runs" / job / "pipeline/manifest.json", {"tasks": {
            "msa": {"state": "submitted", "stage": "msa"},
            "old": {"state": "succeeded", "stage": "fold"}}})
    config = deepcopy(DEFAULTS)
    config["execution"]["runtime_limits_file"] = str(store.limits_path)
    frozen = deepcopy(config)
    plan = {"config": config, "local_output": str(store.root / "runs" / own / "pipeline")}
    with admission(plan) as (limits, others):
        assert limits == {"max_gpu_jobs": 0, "max_msa_jobs": 8}
        assert others == [{"state": "submitted", "stage": "msa"}]
        with open(store.limits_path.with_suffix(".lock"), "a") as second:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.scheduler_limits({"max_gpu_jobs": 2, "max_msa_jobs": 1})
    with admission(plan) as (limits, others):
        assert limits == {"max_gpu_jobs": 2, "max_msa_jobs": 1}
    assert config == frozen
    with pytest.raises(ValueError):
        store.scheduler_limits({"max_gpu_jobs": -1, "max_msa_jobs": 8})
    assert read_json(store.limits_path) == {"max_gpu_jobs": 2, "max_msa_jobs": 1}
