"""Keep background controllers off short-lived WSL interop sessions."""
import os
from pathlib import Path
import stat


def configure(environ=None, release_path=Path('/proc/sys/kernel/osrelease'),
              system_path=Path('/run/WSL/1_interop')):
    """Select only WSL's root-owned system socket; ordinary Linux is unchanged."""
    environ = os.environ if environ is None else environ
    try:
        if 'microsoft' not in release_path.read_text().lower():
            return None
        endpoint = system_path.resolve(strict=True)
        metadata = endpoint.stat()
        if endpoint.parent != system_path.parent or metadata.st_uid != 0 or not stat.S_ISSOCK(metadata.st_mode):
            return None
    except OSError:
        return None
    selected = str(endpoint)
    if environ.get('WSL_INTEROP') != selected:
        environ['WSL_INTEROP'] = selected
        print('Controller using WSL system interop socket: ' + selected, flush=True)
    return selected
