from copy import deepcopy
import subprocess
import pytest
from antigen_design.io import read_json, write_json
from antigen_design.web_ui import initialization


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = {'output': str(tmp_path/'pipeline'), 'msa': {'database_version': 'frozen'},
        'folding': {'model': 'protenix-v2'}, 'execution': {'msa_remote': {
            'enabled': True, 'fallback_when_busy': True, 'host': 'remote'}}}
    settings = {'msa_backends': ['primary', 'remote'], 'gpu_targets': ['gpu']}
    monkeypatch.setattr(initialization, 'repo_for', lambda p: tmp_path)
    monkeypatch.setattr(initialization, 'policy', lambda p: settings)
    monkeypatch.setattr(initialization.time, 'sleep', lambda _: None)
    calls = []
    class Transport:
        def execute(self, host, args, timeout):
            assert args == ['true']
            calls.append(host)
            raise subprocess.CalledProcessError(255, 'ssh', stderr='connection timed out')
    monkeypatch.setattr(initialization, 'get_transport', lambda c: Transport())
    return tmp_path, config, settings, calls


def test_unreachable_optional_host_is_excluded_before_plan_freeze(case):
    root, config, settings, calls = case
    original = deepcopy(config)
    effective = initialization.prepare(config, root)
    assert config == original and settings['msa_backends'] == ['primary', 'remote']
    expected = deepcopy(original); expected['execution']['msa_remote']['enabled'] = False
    assert effective == expected
    assert len(calls) == 3
    assert not (root/'pipeline').exists()
    saved = read_json(root/'initialization.json')
    assert saved['unavailable_backends'] == {'remote': 'connection timed out'}
    assert initialization.prepare(config, root) == expected and len(calls) == 3


def test_globally_disabled_optional_host_is_never_contacted(case):
    root, config, settings, calls = case
    settings['msa_backends'] = ['primary']
    assert initialization.prepare(config, root)['execution']['msa_remote']['enabled'] is False
    assert not calls


@pytest.mark.parametrize('allowed', [False, True])
def test_no_authorized_fallback_fails_before_initialization(case, allowed):
    root, config, settings, calls = case
    if allowed:
        config['execution']['msa_remote']['fallback_when_busy'] = False
    else:
        settings['msa_backends'] = ['remote']
    with pytest.raises(RuntimeError, match='no enabled primary fallback'):
        initialization.prepare(config, root)
    assert not (root/'initialization.json').exists()


def test_reachable_optional_host_preserves_full_configuration(case, monkeypatch):
    root, config, _, _ = case
    class Transport:
        def execute(self, *a, **k): return ''
    monkeypatch.setattr(initialization, 'get_transport', lambda c: Transport())
    assert initialization.prepare(config, root) == config
    assert read_json(root/'initialization.json')['unavailable_backends'] == {}


def test_nonconnection_error_is_not_silently_rerouted(case, monkeypatch):
    root, config, _, _ = case
    class Transport:
        def execute(self, *a, **k): raise subprocess.CalledProcessError(1, 'ssh')
    monkeypatch.setattr(initialization, 'get_transport', lambda c: Transport())
    with pytest.raises(subprocess.CalledProcessError): initialization.prepare(config, root)
    assert not (root/'initialization.json').exists()


@pytest.mark.parametrize('change', ['requested', 'effective', 'policy'])
def test_saved_initialization_cannot_be_rewritten_or_bypass_global_policy(case, change):
    root, config, settings, _ = case
    initialization.prepare(config, root)
    if change == 'requested': config['folding']['model'] = 'different'
    elif change == 'effective':
        saved = read_json(root/'initialization.json')
        saved['effective_config']['folding']['model'] = 'different'
        write_json(root/'initialization.json', saved)
    else: settings['msa_backends'] = ['remote']
    with pytest.raises(ValueError): initialization.prepare(config, root)
