"""Collect complete projects without re-entering the compute scheduler."""
import hashlib
from pathlib import Path
import shutil
import tarfile
from antigen_design.io import digest, fingerprint, read_json, write_json
from .collection_archive import collect, MAX_EXPANDED_BYTES
from .results import result_from_directory


def unpack(archive_path, target, plan, verification):
    files = verification['files']
    if (verification['identity'] != plan['identity']
            or verification['inventory_sha256'] != fingerprint(files)
            or verification['validated_tasks'] != len(plan['tasks'])
            or archive_path.stat().st_size != verification['bytes']
            or digest(archive_path) != verification['sha256']):
        raise ValueError('Collection verification mismatch')
    target.mkdir(exist_ok=True)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        total = sum(m.size for m in members)
        if not 0 <= total <= MAX_EXPANDED_BYTES or total != verification['uncompressed_bytes']:
            raise ValueError('Collection expanded size mismatch or limit exceeded')
        if shutil.disk_usage(target).free < total + 256*1024**2:
            raise ValueError('Insufficient disk space for collection extraction')
        seen = set()
        for member in members:
            relative = Path(member.name)
            path = (target / relative).resolve()
            expected = files.get(member.name)
            if (not member.isfile() or relative.is_absolute() or '..' in relative.parts
                    or not path.is_relative_to(target.resolve()) or member.name in seen
                    or expected is None or member.size != expected['bytes']):
                raise ValueError('Unsafe or unexpected collection archive member')
            seen.add(member.name)
        if seen != set(files):
            raise ValueError('Collection archive inventory is incomplete')
        for member in members:
            path = target / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + '.collecting')
            h = hashlib.sha256()
            with archive.extractfile(member) as source, partial.open('wb') as dest:
                for chunk in iter(lambda: source.read(1024*1024), b''):
                    h.update(chunk)
                    dest.write(chunk)
            if h.hexdigest() != files[member.name]['sha256']:
                raise ValueError('Collected archive member checksum mismatch')
            partial.replace(path)
    remote = Path(plan['config']['output'])
    receipts = {p.parent.name:p for p in target.glob('tasks/*/complete.json')}
    if set(receipts) != set(plan['tasks']):
        raise ValueError('Collection does not contain every planned task receipt')
    internal = external = 0
    for name, receipt_path in receipts.items():
        receipt = read_json(receipt_path)
        if receipt['signature'] != plan['tasks'][name]['signature'] or not receipt['artifacts']:
            raise ValueError('Collected receipt signature mismatch')
        for raw, expected in receipt['artifacts'].items():
            source = Path(raw)
            if '..' in source.parts:
                raise ValueError('Unsafe receipt artifact path')
            if source.is_relative_to(remote):
                local = target / source.relative_to(remote)
                if not local.is_file() or local.is_symlink() or digest(local) != expected:
                    raise ValueError('Collected artifact checksum mismatch')
                internal += 1
            else:
                external += 1  # Validated on the primary host before packing.
    return {'tasks': len(receipts), 'archive_members': len(members),
            'uncompressed_bytes': total, 'local_artifact_hashes': internal,
            'remote_external_artifact_hashes': external}


def extract_results(root, plan):
    archive_path, verification = collect(root / 'pipeline')
    target = root / 'collected'
    report = unpack(archive_path, target, plan, verification)
    results = []
    for metric_path in sorted(target.glob('tasks/metrics_*/metrics.json')):
        for index, _ in enumerate(read_json(metric_path)):
            item = result_from_directory(root, plan, target, metric_path.parent.name, index, len(results))
            write_json(root / f"result-{item['id']}.json", item)
            results.append({k:v for k,v in item.items() if k not in ('residues', 'pae', 'pae_labels')})
    if len(results) != plan['expected_predictions']:
        raise ValueError('Collected prediction count differs from plan')
    write_json(root / 'collection-verification.json', report | {'predictions': len(results),
        'archive_sha256': verification['sha256'],
        'reused_existing_remote_archive': verification['reused_existing_archive']})
    write_json(root / 'results.json', results)
