"""Select reachable, globally enabled MSA resources before a new plan is frozen."""
from copy import deepcopy
from datetime import datetime, timezone
import subprocess
import time
from antigen_design.io import fingerprint, read_json, write_json
from antigen_design.transport import get_transport
from .resource_policy import policy, repo_for


def prepare(config, root):
    settings = policy(repo_for({'local_output': config['output']}))
    saved_path = root / 'initialization.json'
    original = fingerprint(config)
    if saved_path.exists():
        saved = read_json(saved_path)
        effective = saved['effective_config']
        if (saved['requested_config_sha256'] != original
                or saved['effective_config_sha256'] != fingerprint(effective)):
            raise ValueError('Initialization configuration changed; preserve this run')
        if not effective['execution'].get('msa_remote', {}).get('enabled') and 'primary' not in settings['msa_backends']:
            raise ValueError('The frozen primary MSA resource is disabled by global policy')
        return effective
    effective = deepcopy(config)
    remote = effective['execution'].get('msa_remote', {})
    unavailable = {}
    if remote.get('enabled'):
        if 'remote' not in settings['msa_backends']:
            unavailable['remote'] = 'Disabled by global resource configuration'
        else:
            transport = get_transport(config)
            for attempt in range(1, 4):
                try:
                    transport.execute(remote['host'], ['true'], timeout=20)
                    break
                except (OSError, subprocess.SubprocessError) as exc:
                    if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                        raise
                    detail = getattr(exc, 'stderr', '') or str(exc)
                    if isinstance(detail, bytes):
                        detail = detail.decode(errors='replace')
                    if attempt == 3:
                        unavailable['remote'] = detail[-1500:]
                    else:
                        time.sleep(attempt)
        if unavailable:
            if 'primary' not in settings['msa_backends'] or not remote.get('fallback_when_busy'):
                raise RuntimeError('Optional MSA host is unavailable and no enabled primary fallback is configured: '
                                   + unavailable['remote'])
            remote['enabled'] = False
            print('Initialization: optional MSA host unavailable; freezing the enabled primary MSA configuration', flush=True)
    elif 'primary' not in settings['msa_backends']:
        raise ValueError('Primary MSA is disabled by global resource configuration')
    report = {'at': datetime.now(timezone.utc).isoformat(), 'requested_config_sha256': original,
        'effective_config_sha256': fingerprint(effective), 'effective_config': effective,
        'global_resources': settings, 'unavailable_backends': unavailable,
        'scope': 'Connectivity and resource selection only; real environment preflight still required'}
    write_json(saved_path, report)
    return effective
