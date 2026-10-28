from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import stat
import pytest
from antigen_design.web_ui.wsl_bridge import configure


def paths(release='6.6-microsoft-standard-WSL2', uid=0, mode=stat.S_IFSOCK, parent='/run/WSL'):
    kernel=Mock()
    kernel.read_text.return_value=release
    endpoint=Mock()
    endpoint.parent=Path(parent)
    endpoint.stat.return_value=SimpleNamespace(st_uid=uid, st_mode=mode)
    endpoint.__str__=Mock(return_value='/run/WSL/2_interop')
    candidate=Mock()
    candidate.parent=Path('/run/WSL')
    candidate.resolve.return_value=endpoint
    return kernel,candidate


def test_background_session_selects_system_socket_preserving_other_environment():
    release,candidate=paths()
    env={'WSL_INTEROP':'/run/WSL/dead_session_interop','WSLENV':'PYTHONIOENCODING','OTHER':'value'}
    assert configure(env,release,candidate)=='/run/WSL/2_interop'
    assert env=={'WSL_INTEROP':'/run/WSL/2_interop','WSLENV':'PYTHONIOENCODING','OTHER':'value'}
    assert configure(env,release,candidate)=='/run/WSL/2_interop'


@pytest.mark.parametrize('kwargs',[
    {'release':'6.6-generic'}, {'uid':1000}, {'mode':stat.S_IFREG}, {'parent':'/tmp'}])
def test_linux_or_untrusted_endpoint_does_not_replace_existing_session(kwargs):
    release,candidate=paths(**kwargs)
    env={'WSL_INTEROP':'original'}
    assert configure(env,release,candidate) is None
    assert env['WSL_INTEROP']=='original'


def test_missing_system_socket_preserves_environment():
    release,candidate=paths()
    candidate.resolve.side_effect=FileNotFoundError()
    env={}
    assert configure(env,release,candidate) is None
    assert env=={}
