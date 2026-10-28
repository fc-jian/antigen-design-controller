"""Setuptools backend that strips local account metadata from source archives."""
import copy
import gzip
import os
from pathlib import Path
import tarfile
import tempfile


def __getattr__(name):
    if name not in {
        'build_wheel', 'build_editable',
        'get_requires_for_build_wheel', 'get_requires_for_build_sdist',
        'get_requires_for_build_editable', 'prepare_metadata_for_build_wheel',
        'prepare_metadata_for_build_editable',
    }:
        raise AttributeError(name)
    from setuptools import build_meta
    return getattr(build_meta, name)


def normalize_sdist(path):
    path = Path(path)
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tar.gz', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with tarfile.open(path, 'r:gz') as source, temporary.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as zipped:
                with tarfile.open(fileobj=zipped, mode='w', format=tarfile.PAX_FORMAT) as target:
                    for original in source:
                        member = copy.copy(original)
                        member.uid = member.gid = 0
                        member.uname = member.gname = ''
                        member.mtime = epoch
                        member.pax_headers = {k: v for k, v in original.pax_headers.items()
                            if k not in ('uid', 'gid', 'uname', 'gname', 'atime', 'ctime', 'mtime')}
                        if member.isfile():
                            with source.extractfile(original) as content:
                                target.addfile(member, content)
                        else:
                            target.addfile(member)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_sdist(sdist_directory, config_settings=None):
    from setuptools import build_meta
    name = build_meta.build_sdist(sdist_directory, config_settings)
    normalize_sdist(Path(sdist_directory) / name)
    return name
