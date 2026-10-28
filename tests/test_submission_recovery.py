import json
import subprocess
import fcntl
import getpass
import pytest
from antigen_design.io import write_json, read_json
from antigen_design.web_ui.submission_recovery import recover_remote, recover
from antigen_design import remote_control, msa_pool


@pytest.fixture
def case(tmp_path, monkeypatch):
    p = {'identity': 'abcdef0123', 'config': {'output': str(tmp_path)},
         'tasks': {'fold_test': {'stage': 'fold'}}}
    path = tmp_path / 'plan.json'
    write_json(path, p)
    (tmp_path / 'control').mkdir()
    monkeypatch.setattr(msa_pool, 'enabled', lambda _: False)
    responses = {'sacct': '', 'squeue': ''}
    monkeypatch.setattr(subprocess, 'check_output', lambda argv, **kw: responses[argv[0]])
    request = {'task': 'fold_test', 'attempt': 1}
    record = tmp_path / 'control/fold_test_a1.json'
    calls = []
    def submit(plan, req):
        calls.append('submit')
        write_json(record, {'job_id': '123'})
        return {'job_id': '123'}
    monkeypatch.setattr(remote_control, 'submit', submit)
    return p, path, request, record, responses, calls


def row(case, job='123', owner=None):
    p = case[0]
    return f"{job}|ah_abcdef01_fold_test_a1|{p['config']['output']}|{owner or getpass.getuser()}\n"


def test_absent_journal_allows_same_attempt_once_then_reconciles(case):
    p, path, req, record, responses, calls = case
    assert recover_remote(str(path), req, p['identity']) == {'job_id': '123'}
    responses['sacct'] = row(case)
    responses['squeue'] = row(case)
    # Actual frozen reconcile verifies identity and preserves attempt / job ID.
    assert recover_remote(str(path), req, p['identity'])['job_id'] == '123'
    assert calls == ['submit']
    assert read_json(record)['reconciled']


def test_unknown_journal_without_accounting_never_replays(case):
    p, path, req, record, _, calls = case
    write_json(record, {'state': 'submitting'})
    with pytest.raises(ValueError, match='unresolved'):
        recover_remote(str(path), req, p['identity'])
    assert calls == []


def test_unknown_journal_with_exact_job_reconciles(case):
    p, path, req, record, responses, calls = case
    write_json(record, {'state': 'submitting'})
    responses['sacct'] = row(case)
    assert recover_remote(str(path), req, p['identity'])['job_id'] == '123'
    assert calls == []


@pytest.mark.parametrize('kind', ['no-journal-job', 'wrong-owner', 'multiple', 'output', 'known-job-mismatch'])
def test_conflicting_evidence_refuses(case, kind):
    p, path, req, record, responses, calls = case
    if kind == 'output':
        (path.parent / 'tasks/fold_test').mkdir(parents=True)
    else:
        responses['sacct'] = row(case, owner='different-owner' if kind == 'wrong-owner' else None)
    if kind == 'multiple':
        responses['sacct'] += row(case, '124')
    if kind == 'known-job-mismatch':
        write_json(record, {'job_id': '999'})
    with pytest.raises(ValueError):
        recover_remote(str(path), req, p['identity'])
    assert calls == []


def test_live_submit_lock_prevents_recovery(case):
    p, path, req, _, _, calls = case
    with (path.parent / 'control/fold_test.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            recover_remote(str(path), req, p['identity'])
    assert calls == []


def test_pool_and_changed_identity_refused(case, monkeypatch):
    p, path, req, _, _, calls = case
    with pytest.raises(ValueError, match='identity'):
        recover_remote(str(path), req, 'changed')
    monkeypatch.setattr(msa_pool, 'enabled', lambda _: True)
    with pytest.raises(ValueError, match='pooled'):
        recover_remote(str(path), req, p['identity'])
    assert calls == []


def test_transport_recovery_is_bounded(case, monkeypatch):
    p = case[0]
    p['config']['execution'] = {'python': {'metrics': '/python'}}
    calls = []
    class Offline:
        def execute(self, *args, **kwargs):
            calls.append(1)
            raise subprocess.CalledProcessError(255, 'ssh')
    monkeypatch.setattr('antigen_design.web_ui.submission_recovery.time.sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='preserve ambiguity'):
        recover(Offline(), p, 'host', case[2])
    assert len(calls) == 3


def test_response_lost_after_acceptance_recovers_same_job(case, monkeypatch):
    p, path, req, _, responses, calls = case
    p['config']['execution'] = {'python': {'metrics': '/python'}}
    transport_calls = []
    class LostConfirmation:
        def execute(self, *args, **kwargs):
            result = recover_remote(str(path), req, p['identity'])
            transport_calls.append(1)
            if len(transport_calls) == 1:
                responses['sacct'] = row(case)
                raise subprocess.CalledProcessError(255, 'ssh')
            return json.dumps(result)
    monkeypatch.setattr('antigen_design.web_ui.submission_recovery.time.sleep', lambda _: None)
    assert recover(LostConfirmation(), p, 'host', req)['job_id'] == '123'
    assert len(transport_calls) == 2
    assert calls == ['submit']


def test_hook_recovers_only_uncertain_submit(monkeypatch):
    from antigen_design import remote
    from antigen_design.web_ui import submission_recovery as module
    calls = []
    def original(*args, **kwargs):
        raise RuntimeError('outcome unknown, reconcile before retry')
    monkeypatch.setattr(remote, 'control_raw', original)
    monkeypatch.setattr(module, 'recover', lambda *args: calls.append(args) or {'job_id': '123'})
    module.install()
    installed = remote.control_raw
    module.install()
    assert remote.control_raw is installed
    assert remote.control_raw(None, {}, 'host', 'submit', {}) == {'job_id': '123'}
    with pytest.raises(RuntimeError):
        remote.control_raw(None, {}, 'host', 'cancel', {})
    assert len(calls) == 1
