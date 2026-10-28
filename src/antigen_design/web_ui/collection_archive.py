"""Locked, verified result collection; retrying never submits computation."""
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import time
from antigen_design.io import digest, read_json, write_json
from antigen_design.transport import get_transport

MAX_EXPANDED_BYTES = 16 * 1024**3


def collect_remote(plan_path, identity):
    import fcntl
    import gzip
    import hashlib
    import os
    from pathlib import Path
    import tarfile
    import time
    import zlib
    from antigen_design.io import digest, fingerprint, read_json, write_json
    from antigen_design.worker import receipt_valid

    plan = read_json(plan_path)
    root = Path(plan['config']['output'])
    if plan['identity'] != identity or Path(plan_path) != root / 'plan.json':
        raise ValueError('Collection plan identity mismatch')
    target, receipt_path = root / 'deliverables.tar.gz', root / 'web-collection.json'
    with (root / '.web-collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Old releases did not lock collection. Never touch their live output.
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                args = (proc / 'cmdline').read_bytes().decode().split('\0')
            except (OSError, UnicodeDecodeError):
                continue
            if 'antigen_design.remote_control' in args:
                i = args.index('antigen_design.remote_control')
                if args[i+1:i+3] == ['collect', str(plan_path)]:
                    raise RuntimeError('Legacy collection still running; reconcile before collecting')
        for name, task in plan['tasks'].items():
            if not receipt_valid(root / 'tasks' / name, task['signature']):
                raise ValueError('Invalid completed task receipt: ' + name)
        files = {}
        paths = []
        for base in ('tasks', 'logs', 'control', 'scripts'):
            paths.extend(p for p in sorted((root / base).rglob('*')) if p.is_file())
        paths += [root / 'plan.json', root / Path(plan['config']['input']).name]
        if plan.get('mode') != 'folding':
            paths.append(root / 'reference.pdb')
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Unsafe collection source')
            files[str(path.relative_to(root))] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
        inventory = fingerprint(files)
        total = sum(v['bytes'] for v in files.values())

        def verified_archive(path):
            seen = set()
            with gzip.open(path, 'rb') as compressed:
                with tarfile.open(fileobj=compressed, mode='r|') as archive:
                    for member in archive:
                        expected = files.get(member.name)
                        if (not member.isfile() or member.name in seen or expected is None
                                or member.size != expected['bytes']):
                            raise ValueError('Collection archive inventory mismatch')
                        seen.add(member.name)
                        h = hashlib.sha256()
                        with archive.extractfile(member) as stream:
                            for chunk in iter(lambda: stream.read(1024*1024), b''):
                                h.update(chunk)
                        if h.hexdigest() != expected['sha256']:
                            raise ValueError('Collection archive member checksum mismatch')
                # Read the gzip trailer too; a valid tar prefix is insufficient.
                while compressed.read(1024*1024):
                    pass
            if seen != set(files):
                raise ValueError('Collection archive is incomplete')

        if receipt_path.exists():
            saved = read_json(receipt_path)
            if (saved.get('version') != 1 or saved.get('identity') != identity
                    or saved.get('path') != str(target) or saved.get('inventory_sha256') != inventory
                    or saved.get('files') != files or saved.get('uncompressed_bytes') != total
                    or saved.get('validated_tasks') != len(plan['tasks'])
                    or saved.get('bytes') != target.stat().st_size or saved.get('sha256') != digest(target)):
                raise ValueError('Saved collection receipt or archive mismatch')
            return saved

        reused = False
        preserved = None
        if target.exists():
            try:
                verified_archive(target)
                reused = True
            except (OSError, EOFError, tarfile.TarError, ValueError, zlib.error):
                preserved = str(root / ('deliverables.interrupted.' + str(time.time_ns()) + '.tar.gz'))
                target.rename(preserved)
        if not reused:
            partial = root / ('deliverables.' + str(os.getpid()) + '.partial.tar.gz')
            with tarfile.open(partial, 'w:gz', compresslevel=1, dereference=True) as archive:
                for name in files:
                    archive.add(root / name, arcname=name, recursive=False)
            verified_archive(partial)
            os.replace(partial, target)
        result = {'version': 1, 'identity': identity, 'path': str(target), 'sha256': digest(target),
            'bytes': target.stat().st_size, 'uncompressed_bytes': total, 'files': files,
            'inventory_sha256': inventory, 'validated_tasks': len(plan['tasks']),
            'reused_existing_archive': reused, 'preserved_incomplete_archive': preserved}
        write_json(receipt_path, result)
        return result


def remote_archive(transport, plan):
    execution = plan['config']['execution']
    code = inspect.getsource(collect_remote) + '\nimport json,sys\nprint(json.dumps(collect_remote(sys.argv[1],sys.argv[2])))'
    argv = [execution['python']['metrics'], '-c', code,
            plan['config']['output'] + '/plan.json', plan['identity']]
    if execution.get('source_dir'):
        argv = ['env', 'PYTHONPATH=' + execution['source_dir'], *argv]
    for attempt in range(1, 4):
        try:
            output = transport.execute(execution['cpu_host'], argv, timeout=600)
            result = json.loads(output.strip().splitlines()[-1])
            if (not isinstance(result, dict) or result.get('version') != 1
                    or result.get('identity') != plan['identity']
                    or result.get('path') != plan['config']['output'] + '/deliverables.tar.gz'
                    or result.get('validated_tasks') != len(plan['tasks'])
                    or not isinstance(result.get('files'), dict)):
                raise ValueError('Invalid collection response')
            return result
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            if isinstance(exc, subprocess.CalledProcessError) and exc.returncode != 255:
                raise
            if attempt == 3:
                raise
            print(f'Collection response unavailable ({attempt}/3); reconciling the locked archive', flush=True)
            time.sleep(attempt * 2)


def collect(pipeline):
    pipeline = Path(pipeline)
    plan = read_json(pipeline / 'plan.json')
    transport = get_transport(plan['config'])
    archive = remote_archive(transport, plan)
    total = archive['uncompressed_bytes']
    if not isinstance(total, int) or not 0 <= total <= MAX_EXPANDED_BYTES:
        raise ValueError('Results exceed the verified 16 GiB collection limit')
    target = pipeline / 'deliverables.tar.gz'
    if not target.is_file() or target.stat().st_size != archive['bytes'] or digest(target) != archive['sha256']:
        if shutil.disk_usage(pipeline).free < total + archive['bytes'] + 256*1024**2:
            raise ValueError('Insufficient local disk space for verified result collection')
        partial = pipeline / 'deliverables.download.partial'
        transport.download(plan['config']['execution']['cpu_host'], archive['path'], partial)
        if partial.stat().st_size != archive['bytes'] or digest(partial) != archive['sha256']:
            raise ValueError('Downloaded deliverable checksum mismatch')
        partial.replace(target)
    write_json(pipeline / 'download_verification.json', archive)
    return target, archive
