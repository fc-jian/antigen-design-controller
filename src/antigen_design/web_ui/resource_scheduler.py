"""Live site policy for admission and routing; never rewrites frozen plans."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from pathlib import Path
import time
from antigen_design.io import read_json, write_json
from .resource_policy import repo_for, policy, limits
from .msa_rebalance import capacity, reroute

unavailable_backends = {}


def choose_msa(plan, manifest, name, transport, settings):
    state = manifest['tasks'][name]
    excluded = unavailable_backends.get(str(plan['local_output']), {})
    enabled = [backend for backend in settings['msa_backends'] if backend not in excluded]
    if state.get('route'):
        return 'remote' if 'remote' in enabled else None
    execution = plan['config']['execution']; remote = execution['msa_remote']
    if state.get('msa_destination') == 'primary':
        return 'primary' if 'primary' in enabled else None
    observation = {'checked_at': time.time(), 'policy': 'global-capacity-balance',
                   'unavailable_backends': dict(excluded)}
    try:
        candidates = []
        for backend, host, resources in [('remote', remote['host'], remote['resources']),
                                          ('primary', execution['cpu_host'], execution['resources']['cpu'])]:
            if backend not in enabled:
                continue
            if backend == 'primary' and not remote.get('fallback_when_busy'):
                continue
            nodes = capacity(transport, host, resources)
            from antigen_design.gpu_routing import memory_mb
            slots = sum(min(n['cpus'] // resources['cpus'], int(n['memory_mb'] // memory_mb(resources['memory']))) for n in nodes)
            observation[backend] = {'available_nodes': nodes, 'free_slots': slots}
            if backend == 'remote' and remote.get('pool', {}).get('enabled'):
                from antigen_design.msa_routing import pool_capacity
                pool = pool_capacity(plan, transport)
                observation[backend]['pool'] = pool
                if pool['kind'] == 'busy':
                    slots = 0
                elif pool['kind'] == 'reuse':
                    raw = transport.execute(host, ['sacct', '-X', '-n', '-P', '-j', str(pool['job_id']), '--format=JobIDRaw,State'], timeout=30)
                    rows = [r.split('|')[1].split()[0] for r in raw.splitlines() if r.split('|')[0] == str(pool['job_id'])]
                    if len(rows) != 1:
                        raise ValueError('MSA pool allocation state unknown')
                    # A queued allocation does not constitute usable pool capacity.
                    if rows[0] == 'RUNNING':
                        slots = max(slots, remote['pool']['queue_limit'] - pool['queued_or_running'])
                    elif rows[0] != 'PENDING':
                        raise ValueError('MSA pool allocation changed; recheck admission')
            active = sum(s['stage'] == 'msa' and s['state'] in ('submitted', 'submitting', 'running')
                         and s['host'] == host for s in manifest['tasks'].values())
            if slots > 0:
                candidates.append((active / slots, backend))
        if not candidates:
            state['routing_reason'] = '全局已启用 MSA 资源暂无可分配容量，等待重查'
            return None
        target = min(candidates)[1]
        state.update(msa_destination=target, routing_reason=f'全局容量与活动负载自动分配：{target}')
        return target
    except Exception as exc:
        observation['error'] = str(exc)
        state['routing_reason'] = '资源状态不明，等待重查；不把连接失败当成无资源'
        return None
    finally:
        state['msa_routing'] = observation
        write_json(Path(plan['local_output']) / 'manifest.json', manifest)


def install():
    from antigen_design import remote, gpu_routing, msa_routing, runtime_limits
    if getattr(remote, '_global_resource_policy', False):
        return
    remote._global_resource_policy = True
    original_advance = remote._advance
    original_snapshots = gpu_routing.snapshots
    stores = {}

    def advance(plan, manifest, transport, *args, **kwargs):
        settings = policy(repo_for(plan))
        if (not plan['config']['execution'].get('msa_remote', {}).get('enabled')
                and 'primary' not in settings['msa_backends']
                and any(s['stage'] == 'msa' and s['state'] == 'pending' for s in manifest['tasks'].values())):
            raise ValueError('The frozen primary MSA resource is disabled by global policy')
        reroute(plan, manifest, transport, settings)
        return original_advance(plan, manifest, transport, *args, **kwargs)

    def snapshots(plan, manifest, transport, *args, **kwargs):
        observed = deepcopy(original_snapshots(plan, manifest, transport, *args, **kwargs))
        enabled = policy(repo_for(plan))['gpu_targets']
        for row in observed:
            if row['id'] not in enabled:
                row.update(free_gpus=0, nodes=[], disabled_by_global_policy=True)
        return observed

    def choose(plan, manifest, name, transport):
        return choose_msa(plan, manifest, name, transport, policy(repo_for(plan)))

    @contextmanager
    def admission(plan):
        repo = repo_for(plan)
        with (repo / 'configs/web-resource-admission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            others = []
            from .results import Store
            own = Path(plan['local_output']).resolve()
            for registry in (repo / 'projects').glob('*/runs/web-registry.json'):
                for job in read_json(registry):
                    from antigen_design.web_store import identifier
                    root = registry.parent / identifier(job)
                    if (root / 'pipeline').resolve() == own or not (root / 'pipeline/manifest.json').exists():
                        continue
                    project = registry.parent.parent.name
                    store = stores.setdefault(project, Store(repo, project))
                    _, manifest = store.snapshot(root)
                    others.extend(s for s in manifest['tasks'].values() if s['state'] in runtime_limits.ACTIVE)
            yield limits(repo), others

    remote._advance = advance
    gpu_routing.snapshots = snapshots
    msa_routing.choose = choose
    runtime_limits.admission = admission
