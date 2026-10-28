"""Transfer a single capsule archive between two user-configured SSH hosts."""
from pathlib import Path
import tempfile
import uuid
import re
from .io import digest, write_json


def transfer(transport, primary_host, remote_host, direction, primary, remote, filename, evidence):
    if direction not in ('up', 'down') or filename not in ('source.tar', 'input.tar.gz', 'result.tar.gz'):
        raise ValueError('Invalid capsule transfer')
    if transport is None or not primary_host:
        raise ValueError('OpenSSH relay requires an explicit transport and primary host')
    endpoints = [(primary_host, Path(primary) / filename), (remote_host, Path(remote) / filename)]
    (source_host, source), (target_host, target) = endpoints if direction == 'up' else endpoints[::-1]
    for host, path in endpoints:
        transport.remote_path(host, path)
        if '..' in path.parts:
            raise ValueError('Capsule paths cannot contain parent traversal')
    expected = transport.execute(source_host, ['sha256sum', str(source)]).split()[0]
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid source archive digest')
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.partial')
    with tempfile.TemporaryDirectory(prefix='ssh-relay-', dir=evidence) as directory:
        local = Path(directory) / filename
        transport.download(source_host, source, local)
        if digest(local) != expected:
            raise ValueError('Relay download checksum mismatch')
        transport.execute(target_host, ['mkdir', '-p', str(target.parent)])
        transport.upload(target_host, local, temporary)
        actual = transport.execute(target_host, ['sha256sum', str(temporary)]).split()[0]
        if actual != expected:
            raise ValueError('Relay upload checksum mismatch; partial file retained for diagnosis')
        transport.execute(target_host, ['mv', '--', str(temporary), str(target)])
        write_json(evidence / 'transfer.json', {'version': 1, 'transport': 'openssh-relay',
                   'direction': direction, 'filename': filename, 'sha256': expected,
                   'bytes': local.stat().st_size, 'verified': True})
