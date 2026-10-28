from copy import deepcopy
import pytest
from antigen_design.io import digest, fingerprint, read_json, write_json
from antigen_design.web_ui import completed_imports as imports


@pytest.fixture
def imported(tmp_path):
    root = tmp_path / 'run'
    directory = root / 'tasks/msa'
    directory.mkdir(parents=True)
    plan = {'identity': 'origin', 'config': {'output': str(root)},
            'environment': {'msa': {'backend': 'primary'}},
            'tasks': {'msa': {'stage': 'msa', 'signature': 'original-task'}}}
    capsule = {'msa_capsule': {'route_id': 'route', 'origin_identity': 'origin',
                              'origin_task_signature': 'original-task'},
               'environment': {'msa': {'backend': 'remote'}},
               'tasks': {'msa': {'signature': 'capsule-task'}}}
    write_json(directory / 'capsule-plan.json', capsule)
    write_json(directory / 'worker-receipt.json', {'signature': 'capsule-task',
                                                 'environment': capsule['environment']['msa']})
    write_json(directory / 'runtime.json', {'slurm_job_id': '8'})
    (directory / 'data.a3m').write_text('>test\nMQIF\n')
    receipt = {'signature': 'original-task', 'route_id': 'route', 'job_id': '8',
               'environment': plan['environment']['msa'],
               'artifacts': {str(p): digest(p) for p in directory.iterdir()}}
    write_json(directory / 'complete.json', receipt)
    write_json(root / 'plan.json', plan)
    request = {'msa': {'route_id': 'route', 'job_id': '8', 'capsule_fingerprint': fingerprint(capsule)}}
    return root / 'plan.json', directory, request


def test_complete_primary_copy_is_verified_without_origin(imported):
    plan, _, request = imported
    assert imports.verify_remote(plan, 'origin', request) == {'msa': {'valid': True}}


@pytest.mark.parametrize('damage', ['artifact', 'route', 'job', 'environment', 'capsule', 'worker'])
def test_corruption_or_identity_mismatch_cannot_validate(imported, damage):
    plan, directory, request = imported
    receipt = read_json(directory / 'complete.json')
    if damage == 'artifact':
        (directory / 'data.a3m').write_text('corrupt')
    elif damage == 'route':
        request['msa']['route_id'] = 'different'
    elif damage == 'job':
        request['msa']['job_id'] = '9'
    elif damage == 'environment':
        receipt['environment'] = {'backend': 'changed'}
    elif damage == 'capsule':
        request['msa']['capsule_fingerprint'] = 'different'
    else:
        path = directory / 'worker-receipt.json'
        write_json(path, {'signature': 'capsule-task', 'environment': {'backend': 'changed'}})
        receipt['artifacts'][str(path)] = digest(path)
    write_json(directory / 'complete.json', receipt)
    assert imports.verify_remote(plan, 'origin', request)['msa']['valid'] is False


def test_primary_plan_identity_must_match(imported):
    plan, _, request = imported
    with pytest.raises(ValueError, match='plan identity'):
        imports.verify_remote(plan, 'different', request)


@pytest.fixture
def state():
    return {'stage': 'msa', 'state': 'succeeded', 'imported': True, 'job_id': '8',
            'route': {'kind': 'msa', 'id': 'route', 'plan': {}}}


@pytest.mark.parametrize('change', [{'state': 'submitted'}, {'state': 'submitting'},
                                   {'state': 'failed'}, {'imported': False}, {'job_id': None}])
def test_only_successfully_imported_tasks_qualify(state, change):
    assert imports.eligible(state)
    state.update(change)
    assert not imports.eligible(state)


def test_active_jobs_and_mutations_keep_original_control(state, monkeypatch):
    from antigen_design import remote
    calls = []
    def original(t, p, h, op, request):
        calls.append((op, request))
        return {name: {'slurm_state': 'RUNNING'} for name in request}
    monkeypatch.setattr(remote, 'control', original)
    monkeypatch.setattr(remote, '_completed_msa_imports', False, raising=False)
    monkeypatch.setattr(imports, 'verify', lambda p, r, t: {n: {'valid': True} for n in r})
    imports.install()
    active = deepcopy(state); active.update(state='submitted', imported=False)
    result = remote.control(None, {}, 'origin', 'inspect', {'done': state, 'active': active})
    assert result['done']['valid'] and result['active']['slurm_state'] == 'RUNNING'
    assert list(calls[-1][1]) == ['active']
    for operation in ('submit', 'cancel', 'reconcile'):
        remote.control(None, {}, 'origin', operation, {'done': state})
        assert calls[-1] == (operation, {'done': state})
    result = remote.control(None, {}, 'origin', 'monitor', {'done': state})
    assert result['done']['live_scheduler_query'] is False
