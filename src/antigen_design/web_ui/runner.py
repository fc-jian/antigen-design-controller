"""Web controller entry point with bounded read-only connection recovery."""
import os
from .connections import install


def main():
    from .wsl_bridge import configure
    configure()
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    forwarded = os.environ.get('WSLENV', '').split(':')
    if 'PYTHONIOENCODING' not in forwarded:
        forwarded.append('PYTHONIOENCODING')
    os.environ['WSLENV'] = ':'.join(filter(None, forwarded))
    install()
    from .resource_scheduler import install as install_resources
    install_resources()
    from .submission_recovery import install as install_submission_recovery
    install_submission_recovery()
    from .pack_recovery import install as install_pack_recovery
    install_pack_recovery()
    from .completed_imports import install as install_completed_imports
    install_completed_imports()
    from .controller import main as run
    run()


if __name__ == '__main__':
    main()
