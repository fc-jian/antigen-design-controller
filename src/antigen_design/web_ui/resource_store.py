"""Project requests use the explicitly configured deployment profile."""
from copy import deepcopy
from antigen_design.config import build_plan, validate_config
from antigen_design.deployment import settings, profile
from .results import Store as ResultsStore
from .resource_policy import policy, limits, catalog, msa_catalog


class Store(ResultsStore):
    def resources(self, value=None):
        config = profile(self.repo)
        if value is not None and config is None:
            raise ValueError('请先在 configs/webapp.yaml 设置 server_profile')
        execution = config['execution'] if config else {}
        return {'settings': policy(self.repo, value), 'gpu_catalog': catalog(self.repo),
                'msa_catalog': msa_catalog(self.repo), 'configured': bool(config),
                'default_gpu_host': execution.get('gpu_host', '')}

    def scheduler_limits(self, value=None):
        return limits(self.repo, value)

    def profiles(self):
        config = profile(self.repo)
        if config is None:
            return []
        execution = config['execution']
        return [{'id': 'configured', 'cpu_host': execution['cpu_host'],
                 'gpu_host': execution['gpu_host'], 'resources': execution['resources']}]

    def ui_defaults(self):
        from antigen_design.config import DEFAULTS, merge
        config = profile(self.repo) or merge(DEFAULTS, settings(self.repo)['scientific_defaults'])
        return {'seed': config['seed'], 'seeds': config['folding']['seeds'],
                'samples': config['folding']['samples'], 'backbones': config['rfd3']['samples'],
                'rfd3': config['rfd3']['enabled'], 'sequences': config['mpnn']['sequences'],
                'temperature': config['mpnn']['temperature']}

    def profile_config(self, profile_id):
        config = profile(self.repo)
        if config is None:
            raise ValueError('Set server_profile in configs/webapp.yaml before submitting jobs')
        return deepcopy(config)

    def compile(self, request, input_path, output):
        if request.get('profile') not in (None, 'auto'):
            raise ValueError('请通过全局配置文件 server_profile 选择后端')
        if not self.profiles():
            raise ValueError('尚未配置后端：请在 configs/webapp.yaml 设置 server_profile')
        policy(self.repo)
        config, _ = super().compile(dict(request, profile='configured'), input_path, output)
        config['execution']['runtime_limits_file'] = settings(self.repo)['resource_limits']
        validate_config(config)
        return config, build_plan(config)

    def submit(self, request):
        return super().submit(dict(request, profile=request.get('profile', 'auto')))

    def preview(self, request):
        result = super().preview(request)
        settings = policy(self.repo)
        result['global_resources'] = settings
        result['gpu_routing']['targets'] = [t for t in result['gpu_routing']['targets']
                                           if t['id'] in settings['gpu_targets']]
        return result
