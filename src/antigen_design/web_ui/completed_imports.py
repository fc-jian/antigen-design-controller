"""Validate completed MSA imports on primary storage without the retired origin."""
import inspect
import json
from pathlib import Path
from antigen_design.io import fingerprint


def verify_remote(plan_path, identity, requests):
    from pathlib import Path
    from antigen_design.io import read_json, fingerprint
    from antigen_design.worker import receipt_valid
    plan = read_json(plan_path)
    if plan['identity'] != identity:
        raise ValueError('Primary plan identity mismatch')
    result = {}
    for name, request in requests.items():
        try:
            task = plan['tasks'][name]
            directory = Path(plan['config']['output']) / 'tasks' / name
            receipt = read_json(directory / 'complete.json')
            if task['stage'] != 'msa' or not receipt_valid(directory, task['signature']):
                raise ValueError('Invalid imported MSA artifacts')
            if (receipt['route_id'] != request['route_id'] or str(receipt['job_id']) != request['job_id']
                    or receipt['environment'] != plan['environment']['msa']):
                raise ValueError('Imported MSA receipt identity mismatch')
            for raw in receipt['artifacts']:
                path = Path(raw)
                if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
                    raise ValueError('Imported artifact path outside task')
            capsule = read_json(directory / 'capsule-plan.json')
            if fingerprint(capsule) != request['capsule_fingerprint']:
                raise ValueError('Imported MSA capsule changed')
            if capsule['msa_capsule'] != {'route_id': request['route_id'],
                    'origin_identity': identity, 'origin_task_signature': task['signature']}:
                raise ValueError('Imported MSA lineage mismatch')
            worker = read_json(directory / 'worker-receipt.json')
            if (worker['signature'] != capsule['tasks'][name]['signature']
                    or worker['environment'] != capsule['environment']['msa']):
                raise ValueError('Imported MSA worker environment mismatch')
            if str(read_json(directory / 'runtime.json')['slurm_job_id']) != request['job_id']:
                raise ValueError('Imported MSA worker job mismatch')
            result[name] = {'valid': True}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result[name] = {'valid': False, 'error': str(exc)}
    return result


VERIFY_CODE = inspect.getsource(verify_remote) + '\nimport json,sys; print(json.dumps(verify_remote(sys.argv[1],sys.argv[2],json.loads(sys.argv[3]))))'


def eligible(state):
    route = state.get('route', {})
    return (state.get('stage') == 'msa' and state.get('state') == 'succeeded'
            and state.get('imported') is True and route.get('kind') == 'msa'
            and bool(state.get('job_id')) and 'plan' in route)


def verify(plan, requests, transport):
    execution = plan['config']['execution']
    items = list(requests.items())
    result = {}
    for offset in range(0, len(items), 8):
        payload = {name: {'route_id': state['route']['id'], 'job_id': str(state['job_id']),
                         'capsule_fingerprint': fingerprint(state['route']['plan'])}
                   for name, state in items[offset:offset + 8]}
        args = ['env', 'PYTHONPATH=' + execution['source_dir'], execution['python']['metrics'],
                '-c', VERIFY_CODE, str(Path(plan['config']['output']) / 'plan.json'),
                plan['identity'], json.dumps(payload)]
        response = json.loads(transport.execute(execution['cpu_host'], args, timeout=90).splitlines()[-1])
        if set(response) != set(payload) or any(not isinstance(v, dict) or type(v.get('valid')) is not bool
                                               for v in response.values()):
            raise ValueError('Incomplete imported-result verification response')
        result.update(response)
    return result


def install():
    from antigen_design import remote
    if getattr(remote, '_completed_msa_imports', False):
        return
    original = remote.control

    def control(transport, plan, host, operation, request=None):
        if operation not in ('inspect', 'monitor'):
            return original(transport, plan, host, operation, request)
        imported = {n: s for n, s in (request or {}).items() if eligible(s)}
        if not imported:
            return original(transport, plan, host, operation, request)
        checked = verify(plan, imported, transport)
        result = {}
        for name, info in checked.items():
            state = imported[name]
            state['import_verification'] = {'source': 'primary-storage', **info}
            if operation == 'inspect':
                result[name] = {'slurm_state': 'COMPLETED', 'valid': info['valid'],
                                'verification_source': 'primary-storage'}
                if not info['valid']:
                    result[name]['error'] = {'error': info['error']}
            else:
                result[name] = {'job_id': str(state['job_id']),
                    'state': 'COMPLETED' if info['valid'] else 'ARTIFACT_INVALID',
                    'observation_source': 'verified-import', 'live_scheduler_query': False}
                if not info['valid']:
                    result[name]['error'] = info['error']
        remaining = {n: s for n, s in (request or {}).items() if n not in imported}
        if remaining:
            result.update(original(transport, plan, host, operation, remaining))
        return result

    remote.control = control
    remote._completed_msa_imports = True
