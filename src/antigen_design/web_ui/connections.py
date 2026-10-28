"""Controller-only protocol guard; frozen scientific releases remain unchanged."""
import json
import subprocess
import time
from antigen_design import transport

PROBE_CODE = 'import json,sys; from antigen_design.worker import probe; print(json.dumps(probe(sys.argv[1],json.loads(sys.argv[2]))))'


class RemoteReadError(ConnectionError):
    """A read-only remote control response was unavailable."""


class ControlResponseGuard:
    def _copy_with_retry(self, method, host, source, target):
        for attempt in range(1, 4):
            try:
                return method(host, source, target)
            except (OSError, subprocess.SubprocessError) as exc:
                if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                    raise
                print(f'File transfer on {host} failed ({attempt}/3): {exc}', flush=True)
                if attempt == 3:
                    raise
                time.sleep(attempt)

    def upload(self, host, source, target):
        return self._copy_with_retry(super().upload, host, source, target)

    def download(self, host, source, target):
        return self._copy_with_retry(super().download, host, source, target)

    def execute(self, host, argv, timeout=60):
        args = list(map(str, argv))
        try:
            index = args.index('antigen_design.remote_control')
        except ValueError:
            # Only the exact frozen read-only environment probe is eligible;
            # arbitrary Python commands and mutations must never be replayed.
            from .completed_imports import VERIFY_CODE
            if not any(args[i] == '-c' and args[i+1] in (PROBE_CODE, VERIFY_CODE)
                       for i in range(len(args)-1)):
                return super().execute(host, argv, timeout)
            operation, readonly = ('imported-result-verification' if VERIFY_CODE in args else 'environment-probe'), True
        else:
            operation = args[index + 1]
            readonly = operation in ('inspect', 'monitor')
        limit = 3 if readonly else 1
        for attempt in range(1, limit + 1):
            try:
                result = super().execute(host, argv, timeout)
                lines = result.strip().splitlines()
                if not lines:
                    raise ValueError('empty stdout')
                try:
                    response = json.loads(lines[-1])
                except json.JSONDecodeError as exc:
                    raise ValueError('invalid JSON response') from exc
                if not isinstance(response, dict):
                    raise ValueError('response is not a JSON object')
                return result
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                message = f'Remote control {operation} on {host}: {exc}'
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                    detail = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr
                    message += '; stderr: ' + detail[-2000:]
                if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255 and not readonly:
                    # Preserve diagnostics even when a mutation is not replayable.
                    print(message, flush=True)
                    raise
                if not readonly:
                    # Never replay submit/import/cancel after an uncertain outcome.
                    raise RuntimeError(message + '; outcome unknown, reconcile before retry') from exc
                print(f'{message} (read attempt {attempt}/{limit})', flush=True)
                if attempt == limit:
                    raise RemoteReadError(message) from exc
                time.sleep(attempt)


def install():
    """Opt in at controller startup, without rewriting any frozen run identity."""
    for name in ('OpenSSH', 'SSHSkill'):
        base = getattr(transport, name)
        if not issubclass(base, ControlResponseGuard):
            setattr(transport, name, type(name, (ControlResponseGuard, base), {}))
