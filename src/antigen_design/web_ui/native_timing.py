"""Read native job clocks without modifying frozen workers or scheduler state."""
import json
import math
import threading
import time
from pathlib import Path

from antigen_design.io import fingerprint, read_json, write_json
from antigen_design.transport import get_transport
from .wsl_bridge import configure


TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT'}
READ_TIMING_CODE = '''import json, sys
from antigen_design.io import read_json, fingerprint
from antigen_design.baremetal import checked_record
plan = read_json(sys.argv[1])
request = json.loads(sys.argv[2])
if fingerprint(plan) != request['plan_sha256']:
    raise ValueError('Native timing plan identity mismatch')
record = checked_record(plan, request['task'], request['attempt'], request['job_id'])
keys = ('job_id', 'task', 'attempt', 'state', 'created_at', 'started_at', 'finished_at')
print(json.dumps({key: record.get(key) for key in keys}, allow_nan=False))
'''


def native_task(plan, name, state, observed):
    return (state.get('backend') == 'baremetal'
            or observed.get('backend') == 'baremetal'
            or state.get('route', {}).get('target', {}).get('backend') == 'baremetal'
            or bool(plan.get('config', {}).get('execution', {}).get('native_gpu')))


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def elapsed(record, *, terminal, now=None):
    """Execution time excludes queuing; an unknown end never becomes 'now'."""
    start, end = record.get('started_at'), record.get('finished_at')
    if not timestamp(start):
        return None
    if end is None and not terminal:
        end = time.time() if now is None else now
    if not timestamp(end) or end < start:
        return None
    return end - start


def request_for(plan, name, state):
    route = state.get('route')
    stage = plan['tasks'][name]['stage']
    if route:
        capsule = route.get('plan')
        checksum = fingerprint(capsule) if capsule is not None else route['plan_fingerprint']
        target = route['target']
        python = (target['python'] if stage == 'fold' else target['design']['python'])
        host, source, root = state['host'], route['source_dir'], route['root']
        if host != target['host']:
            raise ValueError('Native timing host differs from the frozen route')
    else:
        execution = plan['config']['execution']
        checksum, python = fingerprint(plan), execution['python'][stage]
        host, source, root = state['host'], execution['source_dir'], plan['config']['output']
    request = {'plan_sha256': checksum, 'task': name,
               'attempt': state['attempts'], 'job_id': state['job_id']}
    return {'host': host, 'source': source, 'python': python, 'root': root, 'request': request}


def validate_record(record, request):
    if any(record.get(key) != request[key] for key in ('task', 'attempt', 'job_id')):
        raise ValueError('Native timing job identity mismatch')
    if record.get('state') not in TERMINAL:
        raise ValueError('Native job has no confirmed terminal record')
    if not timestamp(record.get('finished_at')):
        raise ValueError('Native job has no valid finish time')
    start = record.get('started_at')
    if start is not None and (not timestamp(start) or record['finished_at'] < start):
        raise ValueError('Native job start/end times are invalid')
    return record


class NativeTiming:
    """Cache confirmed terminal clocks by the exact frozen task/attempt/job."""

    def __init__(self):
        self.lock = threading.RLock()
        self.failures = {}

    def record(self, root, plan, name, state):
        query = request_for(plan, name, state)
        key = fingerprint(query)
        path = root / 'web-monitor' / 'native-timing' / (key + '.json')
        with self.lock:
            if path.exists():
                saved = read_json(path)
                if saved.get('query') != query:
                    raise ValueError('Native timing cache identity mismatch')
                return validate_record(saved['record'], query['request'])
            until, error = self.failures.get(key, (0, ''))
            if time.monotonic() < until:
                raise ValueError(error)
            try:
                # The frozen release validates owner, workdir, signature and job
                # identity. This command only reads its plan and control journal.
                configure()
                raw = get_transport(plan['config']).execute(query['host'],
                    ['env', 'PYTHONPATH=' + query['source'], query['python'], '-c', READ_TIMING_CODE,
                     str(Path(query['root']) / 'plan.json'), json.dumps(query['request'])], timeout=30)
                record = validate_record(json.loads(raw.strip().splitlines()[-1]), query['request'])
                write_json(path, {'query': query, 'record': record, 'observed_at': time.time()})
                self.failures.pop(key, None)
                return record
            except Exception as exc:
                self.failures[key] = (time.monotonic() + 30, str(exc))
                raise

    def enrich(self, root, plan, name, state, observation):
        observed = observation or {}
        if not native_task(plan, name, state, observed):
            return observation
        terminal = (observed.get('state') in TERMINAL
                    or state.get('execution_state') in TERMINAL
                    or state.get('state') in ('succeeded', 'failed', 'cancelled'))
        result = dict(observed)
        if not terminal:
            if 'started_at' in result:
                result['elapsed'] = elapsed(result, terminal=False)
            elif result.get('state') in ('PENDING', 'SUBMITTING'):
                result['elapsed'] = None
            return result
        # Never retain an old, increasing terminal elapsed value on read failure.
        result['elapsed'] = None
        try:
            if timestamp(result.get('finished_at')):
                record = result
            else:
                record = self.record(root, plan, name, state)
            result.update({key: record.get(key) for key in ('created_at', 'started_at', 'finished_at')})
            result['elapsed'] = elapsed(record, terminal=True)
            result['timing_source'] = 'native job start/end timestamps'
            result.pop('timing_error', None)
        except Exception as exc:
            result['timing_error'] = '无法核对作业起止时间：' + str(exc)[-1000:]
        return result
