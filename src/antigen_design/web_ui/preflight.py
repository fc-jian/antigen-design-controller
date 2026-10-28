"""Check live execution environments and explicitly retire unavailable origins."""
from copy import deepcopy
import json
from antigen_design.pipeline import doctor
from antigen_design.msa_remote import target_config
from .connections import PROBE_CODE, RemoteReadError
from .completed_imports import eligible, verify
from .resource_policy import policy, repo_for
from .resource_scheduler import unavailable_backends


def checked_preflight(plan, manifest, transport):
    stages = {t['stage'] for t in plan['tasks'].values()}
    config = plan['config']
    remote = config['execution'].get('msa_remote', {})
    if not (remote.get('enabled') and remote.get('fallback_when_busy')
            and 'msa_remote' in plan['environment']):
        checked = doctor(config, remote=True, stages=stages)
        if not checked['ready'] or checked['stages'] != plan['environment']:
            raise ValueError('Environment identity differs from the frozen run: ' + json.dumps(checked))
        return {'checked_environments': checked['stages'], 'unavailable_backends': {}}

    # primary already has its own separately frozen MSA environment. This copy is
    # used only to probe it; the scientific plan and task signatures stay intact.
    primary = deepcopy(config)
    primary['execution']['msa_remote']['enabled'] = False
    checked = doctor(primary, remote=True, stages=stages)
    expected = {k: v for k, v in plan['environment'].items() if k != 'msa_remote'}
    if not checked['ready'] or checked['stages'] != expected:
        raise ValueError('Primary environment identity differs from the frozen run: ' + json.dumps(checked))
    print('Preflight: primary execution environments match the frozen plan', flush=True)

    settings = policy(repo_for(plan))
    unresolved = [n for n, s in manifest['tasks'].items()
                  if s.get('route', {}).get('kind') == 'msa' and not eligible(s)]
    imported = {n: s for n, s in manifest['tasks'].items() if eligible(s)}
    needs_new_msa = any(s.get('stage') == 'msa' and s['state'] != 'succeeded'
                        for s in manifest['tasks'].values())
    unavailable = {}
    if unresolved or (needs_new_msa and 'remote' in settings['msa_backends']):
        target = target_config(config)
        execution = target['execution']
        args = ['env', 'PYTHONPATH=' + execution['source_dir'], execution['python']['msa'],
                '-c', PROBE_CODE, 'msa', json.dumps(target)]
        try:
            live = json.loads(transport.execute(remote['host'], args, timeout=90).splitlines()[-1])
        except RemoteReadError as exc:
            if unresolved or 'primary' not in settings['msa_backends']:
                raise RuntimeError('Unavailable MSA origin is still required; preserve its jobs: '
                                   + ','.join(unresolved) + '; ' + str(exc)) from exc
            unavailable['remote'] = str(exc)
            print('Preflight: remote unavailable; verifying completed imports before primary-only admission', flush=True)
        else:
            if live != plan['environment']['msa_remote'] or live.get('errors'):
                raise ValueError('Remote environment identity differs from the frozen run')
            checked['stages']['msa_remote'] = live
    else:
        unavailable['remote'] = 'No unfinished origin-bound work; backend disabled or MSA complete'

    verified = verify(plan, imported, transport) if imported else {}
    invalid = [n for n, result in verified.items() if not result['valid']]
    if invalid:
        raise ValueError('Invalid primary MSA imports: ' + ','.join(invalid))
    # Controller-local availability only; global user preferences stay intact.
    unavailable_backends[str(plan['local_output'])] = unavailable
    return {'checked_environments': checked['stages'], 'unavailable_backends': unavailable,
            'validated_primary_msa_imports': len(verified),
            'scope': 'Live execution environments plus complete imported-result lineage and hashes'}
