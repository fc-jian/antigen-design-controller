"""Bounded reconciliation of ordinary Slurm submissions, using frozen journals."""
import inspect
import json
import subprocess
import time


def recover_remote(plan_path, request, identity):
    """Runs on the target with its frozen release; never modifies its source."""
    import datetime
    import fcntl
    import getpass
    import subprocess
    from pathlib import Path
    from antigen_design.io import read_json, write_json
    from antigen_design import remote_control, msa_pool
    plan = read_json(plan_path)
    if plan['identity'] != identity or msa_pool.enabled(plan):
        raise ValueError('Recovery identity mismatch or unsupported pooled submission')
    name, attempt = request['task'], request['attempt']
    if name not in plan['tasks'] or not isinstance(attempt, int) or attempt < 1:
        raise ValueError('Invalid submission identity')
    root = Path(plan['config']['output'])
    if Path(plan_path).resolve() != (root / 'plan.json').resolve():
        raise ValueError('Plan location mismatch')
    control = root / 'control'
    control.mkdir(exist_ok=True)
    record = control / f'{name}_a{attempt}.json'
    job_name = f"ah_{identity[:8]}_{name}_a{attempt}"
    expected = [job_name, str(root), getpass.getuser()]
    with (control / f'{name}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_json(record) if record.exists() else None
        since = datetime.datetime.fromtimestamp(Path(plan_path).stat().st_mtime - 86400).strftime('%Y-%m-%dT%H:%M:%S')
        out = subprocess.check_output(['sacct', '-X', '-n', '-P', '-S', since,
            '--name=' + job_name, '--format=JobIDRaw,JobName%200,WorkDir%500,User'], text=True)
        queued = subprocess.check_output(['squeue', '-h', '--name=' + job_name,
            '-o', '%i|%200j|%500Z|%u'], text=True)
        jobs = set()
        for line in (out + '\n' + queued).splitlines():
            if not line.strip():
                continue
            row = [v.strip() for v in line.split('|')][:4]
            if len(row) != 4 or not row[0].isdigit() or row[1:] != expected:
                raise ValueError('Scheduler identity unavailable or mismatched')
            jobs.add(row[0])
        if len(jobs) > 1:
            raise ValueError('Multiple jobs match; manual reconciliation required')
        job = next(iter(jobs), None)
        if previous is not None:
            if previous.get('rejected'):
                if jobs:
                    raise ValueError('Rejected journal conflicts with scheduler')
                return previous
            if not job or previous.get('job_id') not in (None, job):
                raise ValueError('Journal exists but scheduler acceptance is unresolved')
            action = 'reconcile'
        else:
            if jobs or (root / 'tasks' / name).exists():
                raise ValueError('Missing journal conflicts with existing job or output')
            # The frozen submit writes its journal BEFORE invoking sbatch. Absence
            # under that same lock proves sbatch has not been invoked by this attempt.
            action = 'submit-same-attempt'
        audit = {'action': action, 'task': name, 'attempt': attempt, 'job_id': job,
                 'identity': identity, 'checked_at': datetime.datetime.now().isoformat()}
        write_json(control / f'{name}_a{attempt}_recovery.json', audit)
    # Both frozen operations reacquire the SAME task lock. A competing invocation
    # can only return an existing journal or reject contention, never double-submit.
    if action == 'reconcile':
        return remote_control.reconcile(plan, dict(request, job_id=job))
    return remote_control.submit(plan, request)


def recover(transport, plan, host, request, attempts=3):
    execution = plan['config']['execution']
    source = inspect.getsource(recover_remote)
    source += '\nimport json,sys\nprint(json.dumps(recover_remote(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])))\n'
    argv = [execution['python']['metrics'], '-c', source,
            plan['config']['output'] + '/plan.json', json.dumps(request), plan['identity']]
    if execution.get('source_dir'):
        argv = ['env', 'PYTHONPATH=' + execution['source_dir'], *argv]
    for attempt in range(1, attempts + 1):
        try:
            raw = transport.execute(host, argv, timeout=120)
            result = json.loads(raw.strip().splitlines()[-1])
            if not isinstance(result, dict) or not (str(result.get('job_id', '')).isdigit() or result.get('rejected')):
                raise ValueError('Recovery response lacks a job identity or rejection')
            return result
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                raise RuntimeError('Submission recovery refused; preserve ambiguity') from exc
            if attempt == attempts:
                raise RuntimeError('Submission recovery unavailable; preserve ambiguity') from exc
            time.sleep(attempt * 2)


def install():
    from antigen_design import remote
    original = remote.control_raw
    if getattr(original, '_submission_recovery', False):
        return
    def guarded(transport, plan, host, operation, request=None):
        try:
            return original(transport, plan, host, operation, request)
        except RuntimeError as exc:
            if operation != 'submit' or 'outcome unknown, reconcile before retry' not in str(exc):
                raise
            print(f'Submission response lost on {host}; checking journal and exact scheduler identity', flush=True)
            if plan.get('config', {}).get('execution', {}).get('native_gpu'):
                # Native submit returns its durable journal; an ambiguous journal
                # is never replaced by another process or attempt.
                return original(transport, plan, host, operation, request)
            return recover(transport, plan, host, request)
    guarded._submission_recovery = True
    remote.control_raw = guarded
