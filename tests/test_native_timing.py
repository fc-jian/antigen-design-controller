"""Read real-format local journals through the same read-only remote probe."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from antigen_design.io import read_json, write_json
from antigen_design.web_ui import native_timing
from antigen_design.web_ui.results import Store


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job, name = 'a' * 32, 'fold_0'
    root = store.root / 'runs' / job
    root.mkdir()
    remote = tmp_path / 'native'
    capsule = {'identity': 'capsule', 'config': {'output': str(remote)},
               'tasks': {name: {'signature': 'frozen-task'}}}
    write_json(remote / 'plan.json', capsule)
    record = {'job_id': 'native-one', 'task': name, 'attempt': 1, 'uid': os.getuid(),
              'root': str(remote), 'plan_identity': capsule['identity'],
              'task_signature': capsule['tasks'][name]['signature'], 'state': 'COMPLETED',
              'created_at': 50.0, 'started_at': 100.25, 'finished_at': 201.35}
    journal = remote / 'control' / (name + '_a1.json')
    write_json(journal, record)
    state = {'state': 'succeeded', 'stage': 'fold', 'backend': 'baremetal',
             'host': 'native-host', 'job_id': record['job_id'], 'attempts': 1,
             'route': {'id': 'route', 'root': str(remote),
                       'source_dir': str(Path(__file__).resolve().parents[1] / 'src'),
                       'target': {'backend': 'baremetal', 'host': 'native-host', 'python': sys.executable},
                       'plan': capsule}}
    plan = {'identity': 'origin', 'config': {},
            'tasks': {name: {'stage': 'fold', 'host': 'origin-host', 'deps': [], 'signature': 'original'}}}
    observation = {'backend': 'baremetal', 'state': 'COMPLETED', 'job_id': record['job_id'], 'elapsed': 9999}
    write_json(store.registry, [job])
    write_json(root / 'request.json', {'name': 'test', 'mode': 'folding', 'profile': 'auto'})
    write_json(root / 'status.json', {'state': 'succeeded'})
    write_json(root / 'pipeline/plan.json', plan)
    write_json(root / 'pipeline/manifest.json', {'tasks': {name: state}})
    write_json(root / 'monitor.json', {'tasks': {name: observation}})
    calls = []
    class Transport:
        def execute(self, host, argv, timeout):
            calls.append((host, argv))
            assert host == 'native-host' and timeout == 30
            assert argv[0] == 'env' and argv[3:5] == ['-c', native_timing.READ_TIMING_CODE]
            return subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    monkeypatch.setattr(native_timing, 'get_transport', lambda config: Transport())
    return store, job, root, plan, state, observation, journal, calls


@pytest.mark.parametrize('terminal', sorted(native_timing.TERMINAL))
def test_terminal_duration_survives_later_reads_and_web_restart(case, monkeypatch, terminal):
    store, job, root, plan, state, observed, journal, calls = case
    record = read_json(journal)
    record['state'] = terminal
    write_json(journal, record)
    protected = [journal, journal.parents[1] / 'plan.json', root / 'status.json',
                 root / 'pipeline/plan.json', root / 'pipeline/manifest.json', root / 'monitor.json']
    before = {path: path.read_bytes() for path in protected}
    monkeypatch.setattr(native_timing.time, 'time', lambda: 500)
    first = store.detail(job)['tasks'][0]['observation']
    monkeypatch.setattr(native_timing.time, 'time', lambda: 50000)
    again = Store(store.repo).detail(job)['tasks'][0]['observation']
    assert first['elapsed'] == pytest.approx(101.1)
    assert again == first and first['started_at'] == 100.25 and first['finished_at'] == 201.35
    assert len(calls) == 1
    assert {path: path.read_bytes() for path in protected} == before
    assert read_json(root / 'pipeline/manifest.json')['tasks']['fold_0']['attempts'] == 1


def test_running_elapsed_advances_but_queuing_is_not_execution(case, monkeypatch):
    store, _, root, plan, state, _, _, calls = case
    state = dict(state, state='running')
    timing = native_timing.NativeTiming()
    observed = {'backend': 'baremetal', 'state': 'RUNNING', 'started_at': 100, 'elapsed': 1}
    monkeypatch.setattr(native_timing.time, 'time', lambda: 200)
    assert timing.enrich(root, plan, 'fold_0', state, observed)['elapsed'] == 100
    monkeypatch.setattr(native_timing.time, 'time', lambda: 250)
    assert timing.enrich(root, plan, 'fold_0', state, observed)['elapsed'] == 150
    pending = {'backend': 'baremetal', 'state': 'PENDING', 'elapsed': 9999}
    assert timing.enrich(root, plan, 'fold_0', state, pending)['elapsed'] is None
    assert not calls


@pytest.mark.parametrize('field,value', [('job_id', 'other'), ('uid', -1), ('root', '/wrong'),
    ('plan_identity', 'other'), ('task_signature', 'other'), ('attempt', 2)])
def test_native_identity_mismatch_never_displays_stale_duration(case, field, value):
    store, job, root, _, _, _, journal, _ = case
    record = read_json(journal)
    record[field] = value
    write_json(journal, record)
    observed = store.detail(job)['tasks'][0]['observation']
    assert observed['elapsed'] is None and observed['timing_error']
    assert not (root / 'web-monitor').exists()


def test_changed_remote_plan_is_rejected(case):
    store, job, _, _, _, _, journal, _ = case
    remote_plan = journal.parents[1] / 'plan.json'
    value = read_json(remote_plan)
    value['unexpected'] = True
    write_json(remote_plan, value)
    observed = store.detail(job)['tasks'][0]['observation']
    assert observed['elapsed'] is None and observed['timing_error']


def test_cached_timing_cannot_be_reused_for_another_job(case):
    store, job, root, _, _, _, _, calls = case
    assert store.detail(job)['tasks'][0]['observation']['elapsed'] == pytest.approx(101.1)
    manifest = read_json(root / 'pipeline/manifest.json')
    manifest['tasks']['fold_0']['job_id'] = 'native-other'
    write_json(root / 'pipeline/manifest.json', manifest)
    observed = store.detail(job)['tasks'][0]['observation']
    assert observed['elapsed'] is None and observed['timing_error']
    assert len(calls) == 2


@pytest.mark.parametrize('start,end', [(100, None), (100, 99), (float('nan'), 200)])
def test_terminal_missing_or_invalid_clocks_never_use_current_time(start, end):
    assert native_timing.elapsed({'started_at': start, 'finished_at': end}, terminal=True, now=9999) is None


def test_cancelled_before_start_has_no_execution_duration(case):
    store, job, _, _, _, _, journal, _ = case
    record = read_json(journal)
    record.update(state='CANCELLED', started_at=None)
    write_json(journal, record)
    observed = store.detail(job)['tasks'][0]['observation']
    assert observed['elapsed'] is None and observed['finished_at'] == 201.35
    assert 'timing_error' not in observed


def test_connection_failure_is_bounded_and_does_not_restore_increasing_time(case, monkeypatch):
    store, job, root, _, _, _, _, _ = case
    calls = []
    class Offline:
        def execute(self, *args, **kwargs):
            calls.append(args)
            raise OSError('offline')
    monkeypatch.setattr(native_timing, 'get_transport', lambda config: Offline())
    for _ in range(2):
        observed = store.detail(job)['tasks'][0]['observation']
        assert observed['elapsed'] is None and 'offline' in observed['timing_error']
    assert len(calls) == 1 and not (root / 'web-monitor').exists()


def test_existing_monitor_timestamps_need_no_remote_fetch(case):
    store, _, root, plan, state, observed, _, calls = case
    observed.update(started_at=100, finished_at=201)
    assert store._native_timing.enrich(root, plan, 'fold_0', state, observed)['elapsed'] == 101
    assert not calls
