"""Bare-metal scheduler tests use a fake GPU inventory and a non-model worker."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from antigen_design import baremetal
from antigen_design.io import write_json, read_json
from antigen_design.config import DEFAULTS


@pytest.fixture
def native(tmp_path, monkeypatch):
    root = tmp_path / "capsule"
    root.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    inventory = tools / "mthreads-gmi"
    inventory.write_text("#!/bin/sh\nprintf '0, MTT S5000, 81920 [MiB], 0 [MiB], 0 [%%]\\n'\n")
    inventory.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + ":" + os.environ["PATH"])
    wrapper = tools / "python-worker"
    # This branch stands in for model execution, while supervisor/locks are real.
    wrapper.write_text("#!/bin/sh\n"
        'if [ "$2" = antigen_design.worker ]; then\n'
        '  echo started >> worker-starts\n'
        '  exec sleep 30\n'
        'fi\n'
        f'exec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    config = deepcopy(DEFAULTS)
    config.update(output=str(root))
    config["execution"].update(source_dir=str(Path(__file__).resolve().parents[1] / "src"),
        native_gpu={"root": str(tmp_path / "site"), "devices": [0], "model": "MTT S5000"},
        python={"fold": str(wrapper)})
    config["execution"]["resources"]["gpu"] = {"cpus": 1, "memory": "1G", "time": "00:01:00"}
    plan = {"identity": "plan-native", "config": config, "tasks": {"fold": {"signature": "task-sig", "stage": "fold"}}}
    write_json(root / "plan.json", plan)
    yield plan
    # Only the exact supervisor created by this fixture is eligible for cleanup.
    path = root / "control/fold_a1.json"
    if path.exists():
        record = read_json(path)
        if baremetal.alive(record.get("process")):
            baremetal.cancel(plan, {"fold": {"attempts": 1, "job_id": record["job_id"]}})
            wait_state(plan, baremetal.TERMINAL)


def wait_state(plan, states):
    path = baremetal.record_path(plan, "fold", 1)
    until = time.monotonic() + 12
    while time.monotonic() < until:
        if path.exists() and read_json(path)["state"] in states:
            return read_json(path)
        time.sleep(.1)
    raise AssertionError("Supervisor did not reach " + str(states))


def test_duplicate_submit_and_cancel_never_create_a_second_worker(native):
    first = baremetal.submit(native, {"task": "fold", "attempt": 1})
    second = baremetal.submit(native, {"task": "fold", "attempt": 1})
    assert first == second
    wait_state(native, {"RUNNING"})
    root = Path(native["config"]["output"])
    until = time.monotonic() + 5
    while not (root / "worker-starts").exists() and time.monotonic() < until:
        time.sleep(.05)
    assert (root / "worker-starts").read_text().splitlines() == ["started"]
    req = {"fold": {"attempts": 1, "job_id": first["job_id"], "state": "submitting"}}
    info = baremetal.inspect(native, req)["fold"]
    assert info["execution_state"] == "RUNNING" and info["slurm_state"] is None
    assert info["recovered_job_id"] == first["job_id"]
    baremetal.cancel(native, req, pending_only=True)
    assert not baremetal.record_path(native, "fold", 1).with_suffix(".cancel.json").exists()
    assert baremetal.capacity(native["config"]["execution"]["native_gpu"], first["resources"])["free_gpus"] == 0
    with pytest.raises(ValueError, match="identity"):
        baremetal.cancel(native, {"fold": dict(req["fold"], job_id="foreign")})
    baremetal.cancel(native, req)
    assert wait_state(native, {"CANCELLED"})["state"] == "CANCELLED"


def test_ambiguous_launch_returns_existing_journal_without_replay(native, monkeypatch):
    calls = []
    monkeypatch.setattr(baremetal.subprocess, "Popen", lambda *a, **k: calls.append(a))
    first = baremetal.submit(native, {"task": "fold", "attempt": 1})
    assert baremetal.submit(native, {"task": "fold", "attempt": 1}) == first
    assert len(calls) == 1
    info = baremetal.inspect(native, {"fold": {"attempts": 1, "job_id": first["job_id"]}})["fold"]
    assert info["execution_state"] == "UNKNOWN"
    with pytest.raises(ValueError, match="identity unavailable"):
        baremetal.cancel(native, {"fold": {"attempts": 1, "job_id": first["job_id"]}})


def test_capacity_excludes_external_gpu_use(native, monkeypatch):
    settings = native["config"]["execution"]["native_gpu"]
    resources = native["config"]["execution"]["resources"]["gpu"]
    assert baremetal.capacity(settings, resources)["free_gpus"] == 1
    monkeypatch.setattr(baremetal, "gpu_inventory", lambda: [
        {"index": 0, "model": "MTT S5000", "total_mb": 81920, "used_mb": 1, "utilization": 0}])
    assert baremetal.capacity(settings, resources)["free_gpus"] == 0


def test_process_identity_rejects_pid_reuse_and_foreign_owner():
    identity = baremetal.serial_process(os.getpid())
    assert baremetal.alive(identity)
    assert not baremetal.alive(dict(identity, start_ticks="0"))
    assert not baremetal.alive(dict(identity, uid=identity["uid"] + 1))


def test_native_remote_operations_do_not_use_slurm(native, monkeypatch):
    from antigen_design import remote_control
    for name in ("inspect", "submit", "monitor", "cancel"):
        monkeypatch.setattr(baremetal, name, lambda *a: {"native": True})
        assert getattr(remote_control, name)(native, {}) == {"native": True}


def test_native_transport_uses_trusted_gateway_and_direct_primary_copy(monkeypatch):
    from antigen_design.transport import OpenSSH
    config = deepcopy(DEFAULTS)
    target = {"host": "gpu-site-peer", "root": "/owned", "backend": "baremetal",
              "native": {"gateway": "gpu-site", "destination": "192.0.2.20", "transfer_hosts": ["cluster-cpu"]}}
    config["execution"]["gpu_routing"]["targets"] = [target]
    transport = OpenSSH(config)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(subprocess, "run", run)
    transport.execute("gpu-site-peer", ["hostname"])
    assert calls[-1][-2] == "gpu-site"
    assert "StrictHostKeyChecking=yes" in calls[-1][-1]
    assert "192.0.2.20" in calls[-1][-1]
    transport.transfer_native("cluster-cpu", "/source/input.tar", "gpu-site-peer", "/owned/input.tar")
    assert any(c[-2] == "cluster-cpu" and "scp" in c[-1] and "gpu-site:" in c[-1] for c in calls)
    with pytest.raises(ValueError):
        transport.transfer_native("unconfigured", "/source/input.tar", "gpu-site-peer", "/owned/input.tar")


def test_musa_kernel_defaults_preserve_explicit_scientific_parameters():
    from antigen_design.gpu_routing import folding_options, validate_routing
    target = {'id': 'native-gpu', 'host': 'gpu-site', 'root': '/owned/gpu',
              'python': '/env/bin/python', 'weights': '/models/weights', 'data_root': '/models',
              'resources': {'cpus': 1, 'memory': '1G', 'gres': 'gpu:1'}, 'backend': 'baremetal',
              'native': {'root': '/owned/gpu', 'gateway': 'gpu-site', 'destination': None,
                         'devices': [0], 'model': 'MTT S5000', 'transfer_hosts': ['cluster-cpu']},
              'identity': {'fold': 'a' * 64}}
    target['folding_kernel_defaults'] = {'trimul_kernel': 'torch', 'triatt_kernel': 'musa_sdpa'}
    config = deepcopy(DEFAULTS)
    config['folding']['options'] = {'cycle': 10, 'step': 200, 'dtype': 'bf16'}
    original = deepcopy(config)
    effective = folding_options(config, target)
    assert effective == dict(config['folding']['options'], trimul_kernel='torch', triatt_kernel='musa_sdpa')
    assert config == original
    config['folding']['options']['triatt_kernel'] = 'torch'
    assert folding_options(config, target)['triatt_kernel'] == 'torch'
    config['folding']['options']['triatt_kernel'] = 'triattention'
    with pytest.raises(ValueError, match='unavailable'):
        folding_options(config, target)
    target['folding_kernel_defaults']['cycle'] = 3
    with pytest.raises(ValueError, match='only supported'):
        validate_routing({'enabled': True, 'targets': [target]})
