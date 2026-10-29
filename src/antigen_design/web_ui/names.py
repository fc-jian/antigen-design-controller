"""Display names are independent of frozen requests and scientific provenance."""
from antigen_design.io import read_json, write_json


class Names:
    def display_name(self, job, fallback):
        path = self.job_root(job) / 'display-name.json'
        return read_json(path)['name'] if path.exists() else fallback

    def rename(self, job, data):
        root = self.job_root(job)
        if not isinstance(data, dict) or set(data) != {'name'}:
            raise ValueError('仅允许修改项目名称')
        name = data['name']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError('项目名称需为 1–120 个字符')
        name = name.strip()
        write_json(root / 'display-name.json', {'name': name})
        return {'id': job, 'name': name}

    def list_jobs(self, *args, **kwargs):
        page = super().list_jobs(*args, **kwargs)
        for item in page['items']:
            if not item.get('unavailable'):
                item['name'] = self.display_name(item['id'], item['name'])
        return page

