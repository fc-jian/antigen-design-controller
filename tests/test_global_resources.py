"""Resource policy and cancellation races with scheduler doubles only."""
from copy import deepcopy
from pathlib import Path
import shutil
import yaml
import pytest
from antigen_design.config import DEFAULTS
from antigen_design.io import read_json, write_json
from antigen_design.web_ui import resource_policy as policy, msa_rebalance as balance
from antigen_design.web_ui.resource_scheduler import choose_msa


@pytest.fixture
def workspace(tmp_path):
    source = Path(__file__).parents[1] / 'configs'
    shutil.copytree(source / 'servers', tmp_path / 'configs/servers')
    routing = yaml.safe_load((source / 'gpu-targets.example.yaml').read_text())
    routing['targets'].append(dict(routing['targets'][0], id='gpu-secondary', host='gpu-secondary'))
    (tmp_path / 'configs/gpu-targets.yaml').write_text(yaml.safe_dump(routing))
    (tmp_path / 'configs/webapp.yaml').write_text(yaml.safe_dump({
        'server_profile': 'servers/ssh-slurm-template.yaml', 'gpu_targets': 'gpu-targets.yaml',
        'resource_limits': 'web-resource-limits.json', 'max_msa_jobs': 16}))
    return tmp_path


def test_global_config_shared_and_invalid_update_atomic(workspace):
    from antigen_design.web_ui.resource_store import Store
    a, b = Store(workspace, 'a'), Store(workspace, 'b')
    settings = a.resources()['settings']
    settings['msa_backends'] = ['primary']
    a.resources(settings)
    assert b.resources()['settings']['msa_backends'] == ['primary']
    with pytest.raises(ValueError):
        a.resources(dict(settings, gpu_targets=['unknown']))
    assert a.resources()['settings'] == settings
    a.scheduler_limits({'max_gpu_jobs': 4, 'max_msa_jobs': 3})
    assert b.scheduler_limits() == {'max_gpu_jobs': 4, 'max_msa_jobs': 3}


def test_create_without_cluster_and_reject_project_override(workspace):
    from antigen_design.web_ui.resource_store import Store
    store = Store(workspace)
    request = {'name': 'test', 'mode': 'folding', 'input': '>ubq\nMQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG', 'seeds': [101]}
    assert store.preview(request)['expected_predictions'] == 1
    settings = store.resources()['settings']
    settings['gpu_targets'] = ['gpu-secondary']
    store.resources(settings)
    assert [t['id'] for t in store.preview(request)['gpu_routing']['targets']] == ['gpu-secondary']
    with pytest.raises(ValueError, match='全局'):
        store.preview(request | {'profile': 'primary'})
    result = store.submit(request | {'id': 'c' * 32})
    assert result['profile'] == 'auto'
    saved = read_json(store.root / 'configs' / ('c' * 32 + '.json'))
    assert saved['execution']['runtime_limits_file'] == str(workspace / 'configs/web-resource-limits.json')
    assert store.submit(request | {'id': 'c' * 32})['id'] == 'c' * 32


@pytest.fixture
def msa(tmp_path):
    config = deepcopy(DEFAULTS)
    config['execution']['cpu_host'] = 'cluster-cpu'
    config['execution']['msa_remote'].update(enabled=True, fallback_when_busy=True, host='msa-server',
        resources={'cpus': 32, 'memory': '1400G', 'partition': 'compute'})
    config['execution']['msa_remote']['pool']['enabled'] = True
    plan = {'config': config, 'local_output': str(tmp_path), 'environment': {'msa': {}, 'msa_remote': {}}}
    manifest = {'tasks': {'msa': {'stage': 'msa', 'state': 'pending', 'host': 'cluster-cpu'}}}
    return plan, manifest


def test_pending_pool_without_resources_chooses_primary(msa, monkeypatch):
    p, m = msa
    monkeypatch.setattr('antigen_design.web_ui.resource_scheduler.capacity', lambda t, h, r:
        [] if h == 'msa-server' else [{'node': 'primary', 'cpus': 64, 'memory_mb': 1000000}])
    monkeypatch.setattr('antigen_design.msa_routing.pool_capacity', lambda *a: {'kind': 'reuse', 'job_id': '7', 'queued_or_running': 1})
    class Transport:
        def execute(self, *a, **k): return '7|PENDING|\n'
    assert choose_msa(p, m, 'msa', Transport(), {'msa_backends': ['remote', 'primary']}) == 'primary'


def test_unknown_capacity_never_becomes_no_resources(msa, monkeypatch):
    p, m = msa
    def unavailable(*args): raise ConnectionError('offline')
    monkeypatch.setattr('antigen_design.web_ui.resource_scheduler.capacity', unavailable)
    assert choose_msa(p, m, 'msa', None, {'msa_backends': ['remote', 'primary']}) is None
    assert 'msa_destination' not in m['tasks']['msa']


def test_verified_controller_availability_uses_primary_without_touching_origin(msa, monkeypatch):
    p, m = msa
    from antigen_design.web_ui import resource_scheduler
    monkeypatch.setattr(resource_scheduler, 'unavailable_backends',
                        {str(p['local_output']): {'remote': 'SSH connection timeout'}})
    def capacity(t, host, resources):
        assert host == 'cluster-cpu', 'Unavailable backend must not be contacted for new work'
        return [{'node': 'primary', 'cpus': 64, 'memory_mb': 1000000}]
    monkeypatch.setattr(resource_scheduler, 'capacity', capacity)
    settings = {'msa_backends': ['remote', 'primary']}
    assert choose_msa(p, m, 'msa', None, settings) == 'primary'
    assert settings == {'msa_backends': ['remote', 'primary']}
    assert m['tasks']['msa']['msa_routing']['unavailable_backends']['remote'] == 'SSH connection timeout'


def test_move_requires_cancel_receipt_and_preserves_provenance(msa):
    p, m = msa
    m['tasks']['msa'].update(state='submitted', attempts=2, memory_tier=0, job_id='7',
        history=[], pool={'request_id': 'r'}, route={'id': 'capsule'}, host='msa-server')
    before = deepcopy(m)
    assert not balance.move_confirmed(p, m, ['msa'], {'cancelled': False, 'state': 'RUNNING'})
    assert m == before
    with pytest.raises(ValueError):
        balance.move_confirmed(p, m, ['msa'], {'cancelled': True, 'state': 'CANCELLED', 'request_ids': ['other'], 'job_id': '7'})
    assert balance.move_confirmed(p, m, ['msa'], {'cancelled': True, 'state': 'CANCELLED', 'request_ids': ['r'], 'job_id': '7'})
    state = m['tasks']['msa']
    assert state['state'] == 'pending' and state['attempts'] == 2 and state['msa_destination'] == 'primary'
    assert 'job_id' not in state and 'route' not in state
    assert state['history'][0]['job_id'] == '7' and state['history'][0]['route']['id'] == 'capsule'


@pytest.fixture
def pool_remote(tmp_path, monkeypatch):
    from antigen_design import msa_pool
    session = tmp_path / 'pool/sessions/session'
    directory = session / 'requests/r'
    directory.mkdir(parents=True)
    write_json(directory / 'state.json', {'state': 'queued'})
    plan_path = tmp_path / 'plan.json'; write_json(plan_path, {})
    request = {'job_id': '7', 'tasks': [{'plan': str(plan_path), 'task': 'msa', 'attempt': 1}]}
    monkeypatch.setattr(msa_pool, 'load_reference', lambda *a: ({'job_id': '7'}, session, {'id': 'r'}))
    state = {'value': 'PENDING', 'started': 'None', 'race': False, 'calls': []}
    monkeypatch.setattr(msa_pool, 'allocation', lambda *a: {'state': state['value'], 'job_id': '7'})
    def output(argv, **kwargs):
        if argv[0] == 'scontrol': return 'JobState=PENDING Reason=Resources'
        return '7|' + state['started'] + '|\n'
    def cancel(argv, **kwargs):
        state['calls'].append(argv)
        assert argv == ['scancel', '--state=PENDING', '7']
        state['value'] = 'RUNNING' if state['race'] else 'CANCELLED'
    monkeypatch.setattr('subprocess.check_output', output)
    monkeypatch.setattr('subprocess.run', cancel)
    return request, state, session


def test_remote_confirmed_cancellation_is_recoverable(pool_remote):
    request, state, _ = pool_remote
    assert balance.remote_cancel_pool(request)['cancelled']
    assert balance.remote_cancel_pool(request)['cancelled']
    assert len(state['calls']) == 1


def test_start_cancel_race_never_allows_reroute(pool_remote):
    request, state, _ = pool_remote
    state['race'] = True
    assert not balance.remote_cancel_pool(request)['cancelled']


def test_shared_pool_with_other_requests_never_cancelled(pool_remote):
    request, state, session = pool_remote
    write_json(session / 'requests/other/state.json', {'state': 'queued'})
    with pytest.raises(ValueError, match='other active requests'):
        balance.remote_cancel_pool(request)
    assert state['calls'] == []


def test_already_started_pool_cannot_be_reclassified_as_unstarted(pool_remote):
    request, state, _ = pool_remote
    state['started'] = '2026-10-23T01:00:00'
    with pytest.raises(ValueError, match='may have started'):
        balance.remote_cancel_pool(request)


def test_global_admission_counts_other_projects(workspace, monkeypatch):
    from antigen_design import remote, gpu_routing, msa_routing, runtime_limits
    from antigen_design.web_ui import resource_scheduler
    from antigen_design.web_ui.resource_store import Store
    # Restore all install hooks when this test finishes.
    for module, attr in [(remote, '_advance'), (gpu_routing, 'snapshots'),
                          (msa_routing, 'choose'), (runtime_limits, 'admission')]:
        monkeypatch.setattr(module, attr, getattr(module, attr))
    monkeypatch.setattr(remote, '_global_resource_policy', False, raising=False)
    Store(workspace, 'one')
    other = Store(workspace, 'two')
    job = 'd' * 32
    root = other.root / 'runs' / job
    write_json(other.registry, [job])
    write_json(root / 'pipeline/plan.json', {})
    write_json(root / 'pipeline/manifest.json', {'tasks': {'msa': {'stage': 'msa', 'state': 'running'},
        'fold': {'stage': 'fold', 'state': 'succeeded'}}})
    resource_scheduler.install()
    plan = {'local_output': str(workspace / 'projects/one/runs' / ('e' * 32) / 'pipeline')}
    with runtime_limits.admission(plan) as (settings, active):
        assert settings['max_msa_jobs'] == 16
        assert [s['stage'] for s in active] == ['msa']


def test_reconcile_cancel_intent_even_if_resources_change(msa, monkeypatch):
    p, m = msa
    p['config']['execution']['msa_remote'].update(source_dir='/src', python='/python')
    m['tasks']['msa'].update(state='submitted', host='msa-server', job_id='7', attempts=1, memory_tier=0,
        submitted_at=0, history=[], pool={'session': '/pool', 'request_id': 'r'}, msa_reroute_intent={'destination': 'primary'},
        route={'plan': {'config': {'output': '/capsule'}}})
    def no_new_capacity(*args): raise AssertionError('Must reconcile cancellation intent first')
    monkeypatch.setattr(balance, 'capacity', no_new_capacity)
    class Transport:
        def execute(self, *a, **k):
            return '{"cancelled":true,"state":"CANCELLED","job_id":"7","request_ids":["r"]}'
    assert balance.reroute(p, m, Transport(), {'msa_backends': ['primary'], 'queue_grace_seconds': 30}) == ['msa']
