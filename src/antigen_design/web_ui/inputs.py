"""Read original web submissions without touching frozen runs or scheduling jobs."""
from antigen_design.io import digest, read_json


class ProjectInputs:
    def original_input(self, job):
        request = read_json(self.job_root(job) / 'request.json')
        result = {'id': job, 'name': self.display_name(job, request['name']), 'request': request}
        if request.get('mode') == 'design':
            try:
                metadata, _ = self._original_asset(job)
                result['asset'] = metadata
                result['structure_url'] = f'/api/jobs/{job}/input/structure'
                result['download_url'] = f'/api/jobs/{job}/input/file'
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result['asset_error'] = f'原始结构暂不可用：{exc}'
        return result

    def _original_asset(self, job):
        request = read_json(self.job_root(job) / 'request.json')
        if request.get('mode') != 'design':
            raise ValueError('该项目没有上传结构文件')
        metadata = self.asset(request.get('asset'))
        directory = ((self.root / 'inputs') / request['asset']).resolve()
        if not directory.is_relative_to((self.root / 'inputs').resolve()):
            raise ValueError('Invalid input directory')
        original = (directory / metadata['path']).resolve()
        if original.parent != directory or original.suffix.lower() not in ('.pdb', '.cif', '.mmcif'):
            raise ValueError('Invalid input file')
        if metadata['id'] != request['asset'] or digest(original) != metadata['sha256']:
            raise ValueError('原始结构校验失败，请重新上传结构后建立新项目')
        return metadata, original

    def original_input_file(self, job, kind):
        _, original = self._original_asset(job)
        if kind == 'file':
            return original
        if kind != 'structure':
            raise ValueError('Unknown input artifact')
        viewer = (original.parent / 'viewer.pdb').resolve()
        if viewer.parent != original.parent:
            raise ValueError('Invalid structure view path')
        return viewer

    def compile(self, request, input_path, output):
        request = dict(request)
        source = request.pop('source_job', None)
        if source is not None:
            self.job_root(source)
            if source == request.get('id'):
                raise ValueError('新项目必须使用新的运行 ID')
        return super().compile(request, input_path, output)
