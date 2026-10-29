"""Fast list and archive operations independent of result rendering and SSH."""
import fcntl
from antigen_design.io import read_json, write_json
from antigen_design.web_store import now, identifier
from .read_cache import compact_manifest


class Listing:
    def registered(self):
        jobs = self._reads.get(self.registry) if hasattr(self, '_reads') else read_json(self.registry)
        if not isinstance(jobs, list) or any(not isinstance(job, str) for job in jobs):
            raise ValueError('Invalid application registry')
        return list(jobs)

    def manifest(self, root):
        return self._reads.get(root / 'pipeline/manifest.json', compact_manifest,
                               root / 'web-monitor/read-cache/manifest.json')

    def list_jobs(self, archive='current', profile='', state='', offset=0, limit=25):
        active = {'queued', 'starting', 'running', 'collecting', 'cancelling'}
        if archive not in {'current', 'archived', 'all'} or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('Invalid job filters or pagination')
        items, counts = [], dict(total=0, active=0, succeeded=0, problem=0)
        for job in self.registered():
            try:
                root = self.root / 'runs' / identifier(job)
                req, status = (self._reads.get(root / filename) for filename in ('request.json', 'status.json'))
                item = dict(id=job, **{k: req[k] for k in ('name', 'mode', 'profile')}, state=status['state'],
                            **{k: status[k] for k in ('created_at', 'updated_at') if k in status},
                            archived=self._reads.get(root / 'archive.json')['archived'] if (root / 'archive.json').exists() else False)
                if not all(isinstance(item[k], str) for k in ('name', 'mode', 'profile', 'state')):
                    raise ValueError('Invalid run metadata')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                item = self.unavailable(job, exc)
            counts['total'] += 1
            counts['active'] += item['state'] in active
            counts['succeeded'] += item['state'] == 'succeeded'
            counts['problem'] += item['state'] in {'attention', 'failed'}
            if archive != 'all' and item['archived'] != (archive == 'archived'):
                continue
            if profile and item['profile'] != profile:
                continue
            matches = item['state'] in active if state == 'active' else item['state'] in {'attention', 'failed'} if state == 'problem' else item['state'] == state
            if state and not matches:
                continue
            items.append(item)
        page = items[offset:offset + limit]
        for item in page:
            try:
                root = self.root / 'runs' / identifier(item['id'])
                tasks = self.manifest(root)['tasks'].values() if (root / 'pipeline/manifest.json').exists() else []
                item.update(task_count=len(tasks), completed_tasks=sum(t['state'] == 'succeeded' for t in tasks))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                item.update(task_count=0, completed_tasks=0, error=f'Run record unavailable: {exc}', unavailable=True)
        return dict(items=page, total=len(items), offset=offset, limit=limit, counts=counts)

    def archive(self, job, archived):
        root = self.job_root(job)
        with (root / '.runner.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Controller is still active') from None
            if read_json(root / 'status.json')['state'] not in ('succeeded', 'failed', 'cancelled'):
                raise ValueError('Only stopped historical tasks can be archived')
            if (root / 'pipeline/manifest.json').exists() and any(
                    s['state'] in ('submitting', 'submitted', 'running') for s in self.manifest(root)['tasks'].values()):
                raise ValueError('Active or ambiguous scheduler jobs prevent archiving')
            write_json(root / 'archive.json', {'archived': archived, 'updated_at': now()})
        return {'id': job, 'archived': archived}
