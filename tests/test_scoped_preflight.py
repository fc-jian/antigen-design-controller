from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from antigen_design.io import read_json, write_json
from antigen_design.web_ui import preflight
from antigen_design.web_ui.connections import RemoteReadError


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = {'execution': {'cpu_host': 'primary', 'source_dir': '/release',
        'python': {'msa': '/python'},
        'msa_remote': {'host': 'remote', 'enabled': True, 'fallback_when_busy': True}}}
    primary = {'msa': {'backend': 'primary'}, 'fold': {'backend': 'primary'}}
    plan = {'local_output': str(tmp_path), 'config': config,
            'tasks': {'m': {'stage': 'msa'}, 'f': {'stage': 'fold'}},
            'environment': primary | {'msa_remote': {'backend': 'remote', 'errors': []}}}
    manifest = {'tasks': {'new': {'stage': 'msa', 'state': 'pending'},
        'old': {'stage': 'msa', 'state': 'succeeded', 'imported': True, 'job_id': '8',
                'route': {'kind': 'msa', 'id': 'route', 'plan': {}}}}}
    calls = []
    def doctor(c, **kw):
        assert c['execution']['msa_remote']['enabled'] is False
        calls.append(c)
        return {'ready': True, 'stages': deepcopy(primary)}
    monkeypatch.setattr(preflight, 'doctor', doctor)
    monkeypatch.setattr(preflight, 'target_config', lambda c: deepcopy(c))
    monkeypatch.setattr(preflight, 'repo_for', lambda p: tmp_path)
    monkeypatch.setattr(preflight, 'policy', lambda p: {'msa_backends': ['primary', 'remote']})
    monkeypatch.setattr(preflight, 'verify', lambda p, r, t: {n: {'valid': True} for n in r})
    monkeypatch.setattr(preflight, 'unavailable_backends', {})
    class Transport:
        def execute(self, *a, **kw):
            raise RemoteReadError('origin unreachable')
    return plan, manifest, Transport(), calls


def test_offline_retired_origin_keeps_explicit_scope_and_frozen_plan(case):
    plan, manifest, transport, calls = case
    before = deepcopy(plan)
    report = preflight.checked_preflight(plan, manifest, transport)
    assert plan == before and calls
    assert 'msa_remote' not in report['checked_environments']
    assert 'remote' in report['unavailable_backends']
    assert report['validated_primary_msa_imports'] == 1


@pytest.mark.parametrize('state', ['submitted', 'submitting', 'pending', 'failed'])
def test_unresolved_origin_job_prevents_offline_resume(case, state):
    plan, manifest, transport, _ = case
    manifest['tasks']['old'].update(state=state, imported=False)
    with pytest.raises(RuntimeError, match='still required'):
        preflight.checked_preflight(plan, manifest, transport)


def test_no_enabled_primary_fallback_fails_closed(case, monkeypatch):
    plan, manifest, transport, _ = case
    monkeypatch.setattr(preflight, 'policy', lambda p: {'msa_backends': ['remote']})
    with pytest.raises(RuntimeError, match='still required'):
        preflight.checked_preflight(plan, manifest, transport)


def test_invalid_import_cannot_retire_origin(case, monkeypatch):
    plan, manifest, transport, _ = case
    monkeypatch.setattr(preflight, 'verify', lambda p, r, t: {'old': {'valid': False}})
    with pytest.raises(ValueError, match='Invalid primary'):
        preflight.checked_preflight(plan, manifest, transport)


def test_primary_environment_mismatch_is_not_an_outage(case, monkeypatch):
    plan, manifest, transport, _ = case
    monkeypatch.setattr(preflight, 'doctor', lambda *a, **k: {'ready': True, 'stages': {'msa': 'changed'}})
    with pytest.raises(ValueError, match='Primary environment'):
        preflight.checked_preflight(plan, manifest, transport)


def test_remote_environment_mismatch_is_not_an_outage(case):
    plan, manifest, _, _ = case
    transport = SimpleNamespace(execute=lambda *a, **k: json.dumps({'errors': [], 'backend': 'changed'}))
    with pytest.raises(ValueError, match='Remote environment'):
        preflight.checked_preflight(plan, manifest, transport)


def test_live_remote_is_checked_and_not_excluded(case):
    plan, manifest, _, _ = case
    transport = SimpleNamespace(execute=lambda *a, **k: json.dumps(plan['environment']['msa_remote']))
    report = preflight.checked_preflight(plan, manifest, transport)
    assert report['checked_environments'] == plan['environment']
    assert report['unavailable_backends'] == {}


def test_failed_preflight_never_advances_or_submits(tmp_path, monkeypatch):
    from antigen_design.web_ui import controller
    root = tmp_path / 'run'; pipeline = root / 'pipeline'
    pipeline.mkdir(parents=True)
    write_json(pipeline / 'plan.json', {'dry_run': False, 'code_signature': 'code', 'local_input': 'input',
        'input_sha256': 'inputhash', 'tasks': {'m': {'stage': 'msa'}}, 'config': {}})
    write_json(pipeline / 'manifest.json', {'tasks': {'m': {'stage': 'msa', 'state': 'pending'}}})
    write_json(root / 'status.json', {'state': 'attention'})
    monkeypatch.setattr(controller, 'code_signature', lambda: 'code')
    monkeypatch.setattr(controller, 'digest', lambda p: 'inputhash')
    monkeypatch.setattr('antigen_design.transport.get_transport', lambda p: None)
    calls = []
    monkeypatch.setattr('antigen_design.remote.advance', lambda *a, **k: calls.append('advance'))
    def failure(*args):
        raise ValueError('identity mismatch')
    monkeypatch.setattr(controller, 'checked_preflight', failure)
    controller.run(SimpleNamespace(job_root=lambda j: root), 'run')
    assert not calls
    assert read_json(root / 'status.json')['state'] == 'attention'
