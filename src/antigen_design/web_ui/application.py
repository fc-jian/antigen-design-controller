"""Launch guarded controllers while retaining the base application's lock protocol."""
import fcntl
import subprocess
import sys
from antigen_design.io import read_json
from antigen_design.web_store import now
from antigen_design.webapp import Application as BaseApplication


class Application(BaseApplication):
    def manage_once(self):
        errors = {}
        try:
            jobs = self.store.registered()
        except Exception as exc:
            jobs = []
            errors['registry'] = str(exc)
        for job in jobs:
            try:
                with self.lock:
                    root = self.store.job_root(job)
                    if (root / '.maintenance').exists():
                        continue
                    state = read_json(root / 'status.json')['state']
                    process = self.processes.get(job)
                    if process and process.poll() is None:
                        continue
                    if state not in ('queued', 'starting', 'running', 'collecting', 'cancelling'):
                        continue
                    with (root / '.runner.lock').open('a') as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                    with (root / 'controller.log').open('ab') as log:
                        self.processes[job] = subprocess.Popen(
                            [sys.executable, '-u', '-m', 'antigen_design.web_ui.runner',
                             '--repo', str(self.store.repo), '--project', self.store.root.name, job],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as exc:
                errors[str(job)] = str(exc)
        self.manager_health = {**self.manager_health, 'checked_at': now(), 'errors': errors}
