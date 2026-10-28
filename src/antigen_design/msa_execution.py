"""Opt-in CPU MSA execution changes; search flags and candidate IDs stay intact."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from .io import write_json


def database_files(root, db1, db3):
    """Accept monolithic or contiguous split MMseqs data stores, never join them."""
    root = Path(root)
    for db in (db1, db3):
        if not db or Path(db).name != db or db in ('.', '..'):
            raise ValueError('Database names must be simple basenames')
        for suffix in ('', '_seq', '_h'):
            base = root / (db + suffix)
            for ending in ('.dbtype', '.index'):
                if not Path(str(base) + ending).is_file():
                    raise ValueError(f'Missing database metadata: {base}{ending}')
            if not base.is_file():
                shards = sorted(int(p.name[len(base.name)+1:]) for p in root.glob(base.name + '.*')
                                if re.fullmatch(r'\d+', p.name[len(base.name)+1:]) and p.is_file())
                if not shards or shards != list(range(len(shards))):
                    raise ValueError(f'Missing or non-contiguous database shards: {base}')
        if not (root / (db + '.idx')).is_file():
            raise ValueError(f'Missing indexed database: {db}.idx')
    files = sorted(p for p in root.iterdir() if p.name.startswith((db1, db3))
                   and not p.name.endswith(('_mapping', '_taxonomy')) and p.is_file())
    if not files:
        raise ValueError('Empty database')
    return files


def allocated_cpus():
    cpus = sorted(os.sched_getaffinity(0))
    allocated = os.environ.get('SLURM_CPUS_PER_TASK')
    if allocated:
        cpus = cpus[:int(allocated)]
    return cpus


@contextmanager
def staged_database(config, directory, lanes=1):
    opt = config['optimization']
    context = os.environ.get('ANTIGEN_MSA_POOL_CONTEXT')
    if context:
        from .msa_pool import reuse_context
        yield reuse_context(config, context, directory)
        return
    if opt['staging'] == 'none':
        yield config['database']
        return
    started = time.monotonic()
    files = database_files(config['database'], config['db1'], config['db3'])
    hot = {config['db1'] + '.idx', config['db3'] + '.idx'} if opt['staging'] in ('hybrid', 'indices') else set()
    shared = opt['staging'] == 'indices'
    disk_bytes = 0 if shared else sum(p.stat().st_size for p in files if p.name not in hot)
    shm_bytes = sum(p.stat().st_size for p in files if p.name in hot)
    reserve = int(opt['reserve_gib'] * 1024**3)
    disk_root, shm_root = Path(opt['disk_root']), Path(opt['shm_root'])
    disk_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(disk_root).free < disk_bytes + reserve:
        raise ValueError('Insufficient node-local disk for MSA staging')
    if shm_bytes:
        working_bytes = opt.get('working_memory_gib', 256) * lanes * 1024**3
        required_memory = shm_bytes + reserve + working_bytes
        if not opt['memory_budget_gib'] or required_memory > opt['memory_budget_gib'] * 1024**3:
            raise ValueError('Explicit MSA memory budget insufficient for tmpfs indices and search workers')
        allocated_mb = os.environ.get('SLURM_MEM_PER_NODE')
        if allocated_mb and opt['memory_budget_gib'] * 1024 > int(allocated_mb):
            raise ValueError('MSA memory budget exceeds Slurm node allocation')
        available = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        if available < required_memory:
            raise ValueError('Available node memory insufficient for tmpfs and search workers')
        if shutil.disk_usage(shm_root).free < shm_bytes + reserve:
            raise ValueError('Insufficient tmpfs for MSA indices')
    with tempfile.TemporaryDirectory(prefix='antigen-msa-', dir=disk_root) as disk:
        # Create a tmpfs directory only when required; cleanup only our own dirs.
        from contextlib import nullcontext
        manager = tempfile.TemporaryDirectory(prefix='antigen-msa-', dir=shm_root) if shm_bytes else nullcontext(None)
        with manager as shm:
            for source in files:
                if shared and source.name not in hot:
                    (Path(disk) / source.name).symlink_to(source.resolve())
                    continue
                destination = Path(shm if source.name in hot else disk) / source.name
                before = source.stat()
                shutil.copy2(source, destination)
                after = source.stat()
                if destination.stat().st_size != before.st_size or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    raise ValueError(f'Database changed while staging: {source}')
                if source.name in hot:
                    (Path(disk) / source.name).symlink_to(destination)
            write_json(Path(directory) / 'msa_staging.json', {
                'mode': opt['staging'], 'source': config['database'], 'files': len(files),
                'disk_bytes': disk_bytes, 'shm_bytes': shm_bytes,
                'shared_bytes': sum(p.stat().st_size for p in files if p.name not in hot) if shared else 0,
                'working_memory_gib': opt.get('working_memory_gib', 256) * lanes,
                'copy_validation': 'size_and_source_stat; full database hashes verified at publication',
                'elapsed_seconds': time.monotonic() - started})
            yield disk


def search(config, args, env, directory):
    """Bounded per-job queue; no durable cross-job claims or hidden retries."""
    opt = config['optimization']
    env = dict(env)
    for key in ('OMP_NUM_THREADS', 'OMP_PLACES', 'OMP_PROC_BIND'):
        env.pop(key, None)
    query, output = Path(args[1]), Path(args[3])
    entries = []
    for record in query.read_text().split('>')[1:]:
        name, sequence = record.split('\n', 1)
        entries.append((name, sequence.strip()))
    cpus = allocated_cpus()
    if len(cpus) < config['threads']:
        raise ValueError('MSA threads exceed allocated CPU affinity')
    chunks = [entries[i:i+opt['chunk_size']] for i in range(0, len(entries), opt['chunk_size'])]
    lanes = min(opt['lanes'], len(cpus) // config['threads'], len(chunks))
    if lanes < 1:
        raise ValueError('No MSA queries or usable CPU lanes')
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    import queue
    work = queue.Queue()
    for index, chunk in enumerate(chunks):
        work.put((index, chunk))
    # Each worker owns one disjoint CPU set and takes the next available chunk.
    def lane(worker):
        affinity = cpus[worker*config['threads']:(worker+1)*config['threads']]
        while True:
            try:
                index, chunk = work.get_nowait()
            except queue.Empty:
                return
            task = Path(directory) / 'search_chunks' / f'{index:06d}'
            task.mkdir(parents=True, exist_ok=False)
            fasta = task / 'queries.fasta'
            fasta.write_text(''.join(f'>{name}\n{sequence}\n' for name, sequence in chunk))
            argv = list(args)
            argv[1], argv[3] = str(fasta), str(task / 'output')
            argv = ['taskset', '--cpu-list', ','.join(map(str, affinity)), *argv]
            write_json(task / 'command.json', {'argv': argv, 'cpus': affinity})
            with (task / 'search.log').open('w') as log:
                subprocess.run(argv, check=True, env=env, stdout=log, stderr=subprocess.STDOUT)
            expected = {name + '.a3m' for name, _ in chunk}
            observed = {p.name for p in (task / 'output').glob('*.a3m')}
            if observed != expected:
                raise ValueError('MSA chunk output accounting mismatch')
            for name in sorted(expected):
                shutil.copy2(task / 'output' / name, output / name)
    with staged_database(config, directory, lanes=lanes) as database:
        args = list(args)
        args[2] = str(database)
        with ThreadPoolExecutor(max_workers=lanes) as executor:
            list(executor.map(lane, range(lanes)))
    write_json(Path(directory) / 'msa_execution.json', {
        'requested_lanes': opt['lanes'], 'effective_lanes': lanes,
        'threads_per_lane': config['threads'], 'allocated_cpus': cpus,
        'search_invocations': len(chunks), 'queries': len(entries),
        'elapsed_seconds': time.monotonic() - started})
    return len(chunks)
