"""Cancel an exclusively owned, unstarted pool before moving requests to primary."""
import inspect
import json
from pathlib import Path
import time
from antigen_design.io import write_json
from antigen_design.msa_routing import available_nodes


def remote_cancel_pool(request):
    # Executed using the frozen remote package; no release files are modified.
    from pathlib import Path
    import subprocess
    import time
    from antigen_design.io import read_json, write_json, fingerprint
    from antigen_design.msa_pool import load_reference, allocation, locked
    from antigen_design.gpu_routing import fields
    sessions, ids = set(), set()
    for item in request['tasks']:
        plan = read_json(item['plan'])
        rec, session, payload = load_reference(plan, item['task'], item['attempt'])
        if rec.get('job_id') != request['job_id']:
            raise ValueError('Pool job identity changed')
        sessions.add(str(session)); ids.add(payload['id'])
    if len(sessions) != 1 or len(ids) != len(request['tasks']):
        raise ValueError('Ambiguous pool membership')
    session = Path(sessions.pop())
    with locked(session.parent.parent / 'coordinator.lock'):
        observed = allocation(session)
        intent = session / 'resource-reroute.json'
        identity = fingerprint(request)
        if intent.exists() and read_json(intent)['identity'] != identity:
            raise ValueError('Different recovery already owns this pool')
        active = {f.parent.name for f in (session / 'requests').glob('*/state.json')
                  if read_json(f)['state'] in ('queued', 'running')}
        if active - ids:
            raise ValueError('Pool contains other active requests; refusing allocation cancellation')
        if any(read_json(session / 'requests' / rid / 'state.json')['state'] != 'queued' for rid in ids):
            return {'state': observed['state'], 'cancelled': False, 'reason': 'request already started or finished'}
        if observed['state'] == 'PENDING':
            raw = subprocess.check_output(['scontrol', 'show', 'job', request['job_id'], '-o'], text=True)
            job = fields(raw)
            if job.get('JobState') != 'PENDING' or job.get('Reason') != 'Resources':
                return {'state': job.get('JobState'), 'cancelled': False, 'reason': job.get('Reason')}
            # Record intent before mutation, and prohibit later pool admission.
            write_json(intent, {'identity': identity, 'tasks': request['tasks'], 'at': time.time()})
            (session / 'drain.request').touch()
            subprocess.run(['scancel', '--state=PENDING', request['job_id']], check=True)
            for _ in range(10):
                observed = allocation(session)
                if observed['state'] != 'PENDING':
                    break
                time.sleep(1)
        if observed['state'] != 'CANCELLED' or not intent.exists():
            return {'state': observed['state'], 'cancelled': False, 'reason': 'not confirmed cancelled before start'}
        raw = subprocess.check_output(['sacct', '-X', '-n', '-P', '-j', request['job_id'],
                                       '--format=JobIDRaw,Start'], text=True)
        starts = [line.split('|')[1] for line in raw.splitlines() if line.split('|')[0] == request['job_id']]
        if len(starts) != 1 or starts[0] not in ('None', 'Unknown', ''):
            raise ValueError('Pool may have started; manual reconciliation required')
        result = {'cancelled': True, 'state': 'CANCELLED', 'job_id': request['job_id'],
                  'request_ids': sorted(ids), 'verified_at': time.time(), 'start': starts[0]}
        write_json(session / 'resource-reroute-confirmed.json', result)
        return result


def capacity(transport, host, resources):
    nodes = transport.execute(host, ['scontrol', 'show', 'nodes', '-o'], timeout=30)
    partition = transport.execute(host, ['scontrol', 'show', 'partition', resources['partition'], '-o'], timeout=30)
    return available_nodes(nodes, partition, resources)


def move_confirmed(plan, manifest, names, receipt):
    if not receipt.get('cancelled') or receipt.get('state') != 'CANCELLED':
        return False
    expected = {manifest['tasks'][n]['pool']['request_id'] for n in names}
    if set(receipt['request_ids']) != expected:
        raise ValueError('Cancelled request membership mismatch')
    if any(manifest['tasks'][n]['job_id'] != receipt['job_id'] for n in names):
        raise ValueError('Cancelled allocation mismatch')
    for name in names:
        state = manifest['tasks'][name]
        history = state['history']
        history.append({k: v for k, v in state.items() if k != 'history'} |
                       {'slurm_state': 'CANCELLED', 'reroute_evidence': receipt})
        attempts, tier = state['attempts'], state['memory_tier']
        state.clear()
        state.update(state='pending', attempts=attempts, memory_tier=tier, stage='msa',
                     host=plan['config']['execution']['cpu_host'], history=history,
                     msa_destination='primary', routing_reason='remote 无可分配资源；原池确认未启动且已取消，转入 primary')
    return True


def reroute(plan, manifest, transport, settings):
    execution = plan['config']['execution']; remote = execution.get('msa_remote', {})
    if not remote.get('enabled') or not remote.get('fallback_when_busy') or 'primary' not in settings['msa_backends']:
        return []
    if 'msa_remote' not in plan['environment'] or 'msa' not in plan['environment']:
        raise ValueError('Both MSA environments must already be frozen')
    groups = {}
    for name, state in manifest['tasks'].items():
        if state['stage'] == 'msa' and state['state'] in ('submitted', 'running') and state.get('pool'):
            groups.setdefault((state['host'], state['job_id'], state['pool']['session']), []).append(name)
    moved = []
    for (host, job, session), names in groups.items():
        if any(time.time() - manifest['tasks'][n].get('submitted_at', time.time()) < settings['queue_grace_seconds'] for n in names):
            continue
        # Do not conflate a transport error with an exhausted resource pool.
        recovering = any(manifest['tasks'][n].get('msa_reroute_intent') for n in names)
        if not recovering:
            if capacity(transport, host, remote['resources']):
                continue
            if not capacity(transport, execution['cpu_host'], execution['resources']['cpu']):
                continue
        payload = {'job_id': job, 'tasks': sorted([{'task': n, 'attempt': manifest['tasks'][n]['attempts'],
            'plan': str(Path(manifest['tasks'][n]['route']['plan']['config']['output']) / 'plan.json')}
            for n in names], key=lambda item: item['task'])}
        for n in names:
            manifest['tasks'][n]['msa_reroute_intent'] = {'at': time.time(), 'destination': 'primary', 'job_id': job}
        root = Path(plan['local_output'])
        write_json(root / 'manifest.json', manifest)
        code = inspect.getsource(remote_cancel_pool) + '\nimport json,sys\nprint(json.dumps(remote_cancel_pool(json.loads(sys.argv[1]))))'
        raw = transport.execute(host, ['env', 'PYTHONPATH=' + remote['source_dir'], remote['python'],
                                       '-c', code, json.dumps(payload)], timeout=90)
        if not raw.strip():
            raise ConnectionError('Pool cancellation response missing; reconcile the same intent before submission')
        receipt = json.loads(raw.splitlines()[-1])
        write_json(root.parent / ('msa-reroute-' + job + '.json'), receipt)
        if move_confirmed(plan, manifest, names, receipt):
            moved.extend(names)
            write_json(root / 'manifest.json', manifest)
        elif receipt.get('state') in ('RUNNING', 'COMPLETING', 'COMPLETED'):
            for n in names:
                manifest['tasks'][n].pop('msa_reroute_intent', None)
            write_json(root / 'manifest.json', manifest)
    return moved
