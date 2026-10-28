"""Distribution metadata must not leak the machine's account identity."""
import io
import tarfile
from build_backend import normalize_sdist


def test_sdist_scrubs_ownership_and_preserves_file_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '1234')
    path = tmp_path / 'release.tar.gz'
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo('package/README.md')
        member.uid, member.gid = 1001, 1002
        member.uname, member.gname = 'fixture-owner', 'fixture-group'
        member.mtime = 123456.25
        member.pax_headers = {'atime': '123456.25', 'ctime': '123456.25', 'uname': 'fixture-owner'}
        content = b'public documentation\n'
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    normalize_sdist(path)
    first = path.read_bytes()
    with tarfile.open(path) as archive:
        member = archive.getmembers()[0]
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, '', '')
        assert member.mtime == 1234
        assert not set(member.pax_headers) & {'atime', 'ctime', 'uname'}
        assert archive.extractfile(member).read() == content
    normalize_sdist(path)
    assert path.read_bytes() == first
