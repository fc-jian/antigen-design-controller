"""Idempotent capsule packing via a locked, hash-checked transfer receipt."""
import inspect
import json
import subprocess
import time


def pack_remote(plan_path, request, identity):
    import fcntl
    import re
    from pathlib import Path
    from antigen_design.io import read_json, digest, fingerprint
    from antigen_design.gpu_transfer import pack
    plan = read_json(plan_path)
    route, name = request['route'], request['task']
    if plan['identity'] != identity or name not in plan['tasks'] or not re.fullmatch('[0-9a-f]{32}', route['id']):
        raise ValueError('Invalid pack identity')
    base = Path(plan['config']['output']) / 'transfers' / route['id']
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'pack.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = base / 'input-transfer.json'
        if not receipt.exists():
            # Frozen pack refuses partial input directories; never overwrite them.
            return pack(plan, request)
        saved = read_json(receipt)
        capsule = saved['plan']
        meta = capsule['gpu_capsule']
        archive = base / 'input.tar.gz'
        if (saved['path'] != str(archive) or saved['bytes'] != archive.stat().st_size
                or saved['sha256'] != digest(archive)
                or meta['origin_identity'] != identity or meta['route_id'] != route['id']
                or meta['origin_task_signature'] != plan['tasks'][name]['signature']
                or set(capsule['tasks']) != {name}
                or capsule['config']['output'] != route['root']
                or capsule['identity'] != fingerprint({'origin': identity, 'route': route, 'inputs': meta['inputs']})
                or meta['portable'] != route['portable']
                or capsule['environment'] != {plan['tasks'][name]['stage']: route['environment']}):
            raise ValueError('Saved pack receipt identity or checksum mismatch')
        return saved


def pack(transport, plan, host, request):
    execution = plan['config']['execution']
    code = inspect.getsource(pack_remote) + '\nimport json,sys\nprint(json.dumps(pack_remote(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3])))'
    argv = [execution['python']['metrics'], '-c', code, plan['config']['output'] + '/plan.json',
            json.dumps(request), plan['identity']]
    if execution.get('source_dir'):
        argv = ['env', 'PYTHONPATH=' + execution['source_dir'], *argv]
    for attempt in range(3):
        try:
            result = json.loads(transport.execute(host, argv, timeout=300).strip().splitlines()[-1])
            if not isinstance(result, dict) or not all(k in result for k in ('plan', 'path', 'sha256', 'bytes')):
                raise ValueError('Incomplete pack response')
            return result
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            # A remote refusal (partial directory, invalid receipt) is not transient.
            if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                raise
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def install():
    from antigen_design import remote
    previous = remote.control_raw
    if getattr(previous, '_pack_recovery', False):
        return
    def guarded(transport, plan, host, operation, request=None):
        if operation == 'gpu_pack':
            return pack(transport, plan, host, request)
        return previous(transport, plan, host, operation, request)
    guarded._pack_recovery = True
    remote.control_raw = guarded
