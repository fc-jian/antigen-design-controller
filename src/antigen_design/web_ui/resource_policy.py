"""Site-wide mutable admission controls, separate from scientific identities."""
from pathlib import Path
import fcntl
import yaml
from antigen_design.io import read_json, write_json
from antigen_design.runtime_limits import validate as validate_limits
from antigen_design.deployment import settings, profile


def repo_for(plan):
    root = Path(plan['local_output']).resolve()
    for parent in root.parents:
        if parent.name == 'projects':
            return parent.parent
    raise ValueError('Run must belong to a project workspace')


def catalog(repo):
    config = profile(repo)
    routing = config['execution']['gpu_routing'] if config else {}
    return routing.get('targets', []) if routing.get('enabled') else []


def msa_catalog(repo):
    config = profile(repo)
    if config is None:
        return []
    execution = config['execution']
    remote = execution['msa_remote']
    result = []
    if not remote['enabled'] or remote['fallback_when_busy']:
        result.append({'id': 'primary', 'label': execution.get('msa_host') or execution['cpu_host']})
    if remote['enabled']:
        result.append({'id': 'remote', 'label': remote['host']})
    return result


def validate(repo, value):
    if not isinstance(value, dict) or set(value) != {'version', 'gpu_targets', 'msa_backends', 'queue_grace_seconds'}:
        raise ValueError('Invalid global resource settings')
    if value['version'] != 1:
        raise ValueError('Unsupported resource settings version')
    for key, allowed in [('gpu_targets', {t['id'] for t in catalog(repo)}),
                         ('msa_backends', {t['id'] for t in msa_catalog(repo)})]:
        values = value[key]
        if not isinstance(values, list) or (allowed and not values) or any(not isinstance(v, str) for v in values):
            raise ValueError(f'{key} must contain at least one resource')
        if len(set(values)) != len(values) or set(values) - allowed:
            raise ValueError(f'Unknown or duplicate {key}')
    if type(value['queue_grace_seconds']) is not int or not 30 <= value['queue_grace_seconds'] <= 86400:
        raise ValueError('Queue grace must be 30–86400 seconds')
    return value


def policy(repo, value=None):
    repo = Path(repo)
    deployment = settings(repo)
    path = Path(deployment['resource_policy'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if value is not None:
            write_json(path, validate(repo, value))
        if not path.exists():
            initial = {'version': 1, 'gpu_targets': [t['id'] for t in catalog(repo)],
                       'msa_backends': [t['id'] for t in msa_catalog(repo)],
                       'queue_grace_seconds': deployment['queue_grace_seconds']}
            if not initial['msa_backends']:
                return validate(repo, initial)
            write_json(path, initial)
        return validate(repo, read_json(path))


def limits(repo, value=None, initial=None):
    deployment = settings(repo)
    path = Path(deployment['resource_limits'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.settings.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if value is not None:
            write_json(path, validate_limits(value))
            # Old runs retain their frozen path; mirror this mutable policy only.
            for registry in (Path(repo) / 'projects').glob('*/runs/web-registry.json'):
                write_json(registry.parent.parent / 'scheduler-limits.json', validate_limits(value))
        if not path.exists():
            write_json(path, validate_limits(initial or {k: deployment[k] for k in ('max_gpu_jobs', 'max_msa_jobs')}))
        return validate_limits(read_json(path))
