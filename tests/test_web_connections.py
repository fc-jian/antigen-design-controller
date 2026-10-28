import subprocess
import pytest
from antigen_design.web_ui import connections


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def execute(self, host, argv, timeout=60):
        self.calls += 1
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def upload(self, host, source, target):
        return self.execute(host, [source, target])

    download = upload


class Guarded(connections.ControlResponseGuard, FakeTransport):
    pass


def command(op):
    return ['python', '-m', 'antigen_design.remote_control', op, '/run/plan.json', '{}']


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(connections.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('op', ['inspect', 'monitor'])
def test_read_empty_then_success(op):
    client = Guarded([' \n', 'banner\n{"task": {"valid": true}}'])
    assert 'valid' in client.execute('host', command(op))
    assert client.calls == 2


def test_exhausted_read_is_connection_error():
    client = Guarded(['', '', ''])
    with pytest.raises(connections.RemoteReadError, match='empty stdout'):
        client.execute('host', command('inspect'))
    assert client.calls == 3


@pytest.mark.parametrize('op', ['submit', 'cancel', 'gpu_import', 'msa_import'])
@pytest.mark.parametrize('response', ['', 'not json', subprocess.CalledProcessError(255, 'ssh')])
def test_mutation_never_replayed(op, response):
    client = Guarded([response, '{}'])
    with pytest.raises(RuntimeError, match='outcome unknown'):
        client.execute('host', command(op))
    assert client.calls == 1


def test_read_command_failures_retry_but_invalid_artifacts_are_not_hidden():
    client = Guarded([subprocess.CalledProcessError(255, 'ssh'), '{}'])
    assert client.execute('host', command('monitor')) == '{}'
    client = Guarded([subprocess.CalledProcessError(1, 'ssh'), '{}'])
    assert client.execute('host', command('inspect')) == '{}'
    assert client.calls == 2
    client = Guarded(['{"task": {"valid": false}}'])
    assert 'false' in client.execute('host', command('inspect'))
    assert client.calls == 1


def test_non_protocol_empty_output_is_valid():
    client = Guarded([''])
    assert client.execute('host', ['mkdir', '-p', '/run']) == ''


@pytest.mark.parametrize('operation', ['inspect', 'monitor'])
@pytest.mark.parametrize('exit_code', [1, 2, 113, 255])
def test_read_failure_is_bounded_and_retains_diagnostics(operation, exit_code):
    failure = subprocess.CalledProcessError(exit_code, 'ssh', stderr='transport detail')
    client = Guarded([failure, '{}'])
    assert client.execute('host', command(operation)) == '{}'
    assert client.calls == 2
    client = Guarded([failure, failure, failure])
    with pytest.raises(connections.RemoteReadError, match='transport detail'):
        client.execute('host', command(operation))
    assert client.calls == 3


@pytest.mark.parametrize('exit_code', [1, 2, 113])
@pytest.mark.parametrize('operation', ['submit', 'cancel', 'gpu_import', 'msa_import'])
def test_remote_failure_does_not_replay_mutations(exit_code, operation):
    client = Guarded([subprocess.CalledProcessError(exit_code, 'ssh'), '{}'])
    with pytest.raises(subprocess.CalledProcessError):
        client.execute('host', command(operation))
    assert client.calls == 1


@pytest.mark.parametrize('failure', ['', subprocess.TimeoutExpired('ssh', 300), subprocess.CalledProcessError(255, 'ssh'), subprocess.CalledProcessError(1, 'ssh')])
def test_environment_probe_connection_retries(failure):
    client = Guarded([failure, '{"errors": []}'])
    command = ['python', '-c', connections.PROBE_CODE, 'rfd3', '{}']
    assert client.execute('host', command) == '{"errors": []}'
    assert client.calls == 2


def test_actual_environment_errors_are_not_retried_or_hidden():
    client = Guarded(['{"errors": ["checkpoint changed"]}'])
    command = ['python', '-c', connections.PROBE_CODE, 'fold', '{}']
    assert 'checkpoint changed' in client.execute('host', command)
    assert client.calls == 1


def test_exact_import_verifier_is_readonly_but_modified_code_is_not_replayed():
    from antigen_design.web_ui.completed_imports import VERIFY_CODE
    failure = subprocess.CalledProcessError(255, 'ssh')
    client = Guarded([failure, '{"msa": {"valid": false}}'])
    argv = ['python', '-c', VERIFY_CODE, '/run/plan.json', 'identity', '{}']
    assert 'false' in client.execute('host', argv)
    assert client.calls == 2
    client = Guarded([failure, '{}'])
    argv[2] += '\n# different command'
    with pytest.raises(subprocess.CalledProcessError):
        client.execute('host', argv)
    assert client.calls == 1


@pytest.mark.parametrize('method', ['upload', 'download'])
def test_same_file_transfer_retries_connection_failure(method):
    client = Guarded([subprocess.CalledProcessError(255, 'scp'), None])
    assert getattr(client, method)('host', '/source', '/target') is None
    assert client.calls == 2


def test_malformed_read_is_retried():
    client = Guarded(['[]', 'broken', '{}'])
    assert client.execute('host', command('inspect')) == '{}'
    assert client.calls == 3


def test_install_keeps_frozen_source_identity(monkeypatch):
    from antigen_design.worker import code_signature
    original = code_signature()
    monkeypatch.setattr(connections.transport, 'OpenSSH', FakeTransport)
    monkeypatch.setattr(connections.transport, 'SSHSkill', FakeTransport)
    connections.install()
    first = connections.transport.OpenSSH
    connections.install()
    assert connections.transport.OpenSSH is first
    assert code_signature() == original


def test_manager_launches_guarded_runner_once(tmp_path, monkeypatch):
    from antigen_design.web_ui.application import Application
    from antigen_design.web_ui.results import Store
    from antigen_design.io import write_json
    from types import SimpleNamespace
    store = Store(tmp_path)
    job = 'b' * 32
    root = store.root / 'runs' / job
    root.mkdir()
    write_json(store.registry, [job])
    write_json(root / 'status.json', {'state': 'running'})
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr('antigen_design.web_ui.application.subprocess.Popen', spawn)
    app = Application(store, launch=False)
    app.manage_once()
    app.manage_once()
    assert len(calls) == 1
    assert 'antigen_design.web_ui.runner' in calls[0]
    assert app.health()['errors'] == {}
