"""Export one completed folding/metrics pair using the frozen worker receipts."""
import json
import sys
import tarfile
from pathlib import Path
from antigen_design.io import read_json, digest
from antigen_design.worker import receipt_valid

MAX_BYTES = 512 * 1024**2


def export(plan_path, name, identity):
    plan = read_json(plan_path)
    if plan['identity'] != identity:
        raise ValueError('Run identity mismatch')
    task = plan['tasks'][name]
    if task['stage'] != 'metrics' or len(task['deps']) != 1:
        raise ValueError('Select a metrics task')
    fold = task['deps'][0]
    if plan['tasks'][fold]['stage'] != 'fold':
        raise ValueError('Metrics must depend on folding')
    root = Path(plan['config']['output']).resolve()
    files = {}
    for key in (fold, name):
        directory = root / 'tasks' / key
        if not receipt_valid(directory, plan['tasks'][key]['signature']):
            raise ValueError('Completed task receipt is invalid: ' + key)
        receipt = directory / 'complete.json'
        for raw in read_json(receipt)['artifacts']:
            path = Path(raw)
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('Artifact outside the completed task')
            files[str(path.relative_to(root))] = path
        files[str(receipt.relative_to(root))] = receipt
    if sum(p.stat().st_size for p in files.values()) > MAX_BYTES:
        raise ValueError('Individual result exceeds 512 MiB preview limit')
    destination = root / 'web-previews' / task['signature']
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / 'preview.tar.gz'
    temporary = destination / 'preview.tmp'
    with tarfile.open(temporary, 'w:gz') as tar:
        for relative, path in sorted(files.items()):
            tar.add(path, arcname=relative, recursive=False)
    temporary.replace(archive)
    return {'path':str(archive), 'sha256':digest(archive), 'bytes':archive.stat().st_size}


if __name__ == '__main__':
    print(json.dumps(export(*sys.argv[1:])))
