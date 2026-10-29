# Antigen Design / Antigen Studio

可部署在自己机器上的蛋白计算工作台：区域序列设计、可选 RFD3 骨架、独立 MSA、Protenix v2 预测、残基映射、结构指标与可追溯结果。包含中文 webapp 和 Python CLI。工程示例仅使用人源 ubiquitin **PDB 1UBQ**，计算分数不代表实验功能。

本仓库的应用代码使用 [MIT License](LICENSE)。模型、权重、MSA 数据库各自遵循上游条款，均不随仓库分发。特别是 **Protenix v2 权重目前不是开源权重**，需要部署者自行获得适用授权；没有权重也能安装网页、解析输入、验证配置和运行测试。当前适配器固定使用 v2，不自动替换模型。[上游说明](https://github.com/bytedance/Protenix)

## 支持方式

| 部署方式 | 控制器 | 计算资源 |
| --- | --- | --- |
| 本地网页 + SSH 后端 | 本机 Linux 或 Windows WSL2 | 自己控制的 Linux Slurm 服务器/集群 |
| 本机计算 CLI | Linux 或 WSL2 | 本机安装的模型环境、GPU 和数据库 |
| 在 Slurm 集群内运行 CLI | Linux | `backend: slurm, transport: native` |
| 高级路由 | 同一个本地控制器 | 显式配置多个 GPU 站点或独立 MSA 站点 |

网页服务是单用户本地服务，固定监听 `127.0.0.1`。网页后端要求 OpenSSH + Slurm；它不会在普通 SSH 登录节点自动启动模型。已有 MUSA S5000 裸金属 GPU 适配器属于可选高级功能，**不等于支持任意裸金属 GPU**。普通本机推理使用 CLI。

工作流支持独立启动设计、MSA、预测和评估；支持原编号/插入码、多区间、变长映射、缓存验证、候选来源保留、版本化筛选、显式预测导入和提交恢复。webapp 包括 Folding、Design、任务监控、结构预览、pLDDT/PAE 和结果下载。3D 查看器随包提供，不需要 CDN 或 npm 构建。

创建任务时需填写名称；项目详情可修改显示名称，不改变冻结的输入、参数或运行身份。已停止的任务可直接在列表归档和恢复。大型项目的阶段与结果每页显示 50 条，日志和调度详情在展开时加载；切换页面会取消过期的列表/详情请求。

## 1. 安装本地控制器与网页

推荐 Linux / WSL2、Python 3.12。核心要求 Python ≥3.10；后端 Foundry 版本对 Python 有更严格要求。Windows 原生 Python 不支持本项目使用的 POSIX 锁，命令应在 WSL 终端执行。macOS 原生控制器尚未验证，可使用 Linux 虚拟机。

取得仓库后，进入仓库根目录（以下命令均为 Bash）：

```bash
# 将占位地址替换成你实际发布或克隆的仓库地址
git clone REPLACE_WITH_REPOSITORY_URL antigen-design
cd antigen-design
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
cp configs/webapp.example.yaml configs/webapp.yaml
.venv/bin/antigen-design validate projects/1ubq/configs/ubiquitin.yaml
.venv/bin/antigen-web --config configs/webapp.yaml
```

打开 [http://127.0.0.1:8776](http://127.0.0.1:8776)。默认没有启用计算后端；配置好下一节的 SSH profile 后重启服务。安装控制器不会安装或运行模型，也不会下载数据库。

WSL 可直接访问 Windows 的克隆目录，也可把源码放在 WSL 文件系统。浏览器使用 Windows 上的 localhost；若无法连接，先在 WSL 内检查 `curl http://127.0.0.1:8776/api/health`。Python 的 `venv`/`pip` 缺失时，先通过操作系统的软件包管理器安装对应 Python 组件。

## 2. 准备自己的 SSH 后端

### SSH 登录与调度器

后端需提供 Linux、Bash、OpenSSH/SCP、tar、sha256sum，以及 `sbatch`、`squeue`、`sacct`、`scontrol`、`sinfo`、`scancel`。CPU/MSA/GPU 执行节点必须能以**相同绝对路径**访问工作目录、环境、权重和数据库。单一 Slurm 登录主机可同时作为 `cpu_host` 和 `gpu_host`。

在**本地** `~/.ssh/config` 配置自己的别名；示例地址是保留的文档域名：

```sshconfig
Host cluster-login
    HostName cluster.example.org
    User your-login
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

使用现有密钥或按站点要求建立密钥认证。首次连接前从服务器管理方核对 host key，并录入本机 `known_hosts`。先确认无需交互即可登录：

```bash
ssh -o BatchMode=yes cluster-login 'bash -lc "hostname; command -v sbatch; command -v sacct"'
```

密码、私钥、代理令牌不写入 YAML，也不上传到仓库。跳板机可使用 OpenSSH 的 `ProxyJump`；端口和用户名也放在 SSH 配置中。本项目无需任何 agent 平台插件即可使用 OpenSSH。

WSL 默认建议使用 Linux `ssh`/`scp`。如需复用 Windows SSH 配置，在服务器 YAML 中显式设置：

```yaml
execution:
  ssh_binary: /mnt/c/Windows/System32/OpenSSH/ssh.exe
  scp_binary: /mnt/c/Windows/System32/OpenSSH/scp.exe
```

此时使用 Windows 自己的 SSH 配置和 known_hosts，两个程序必须成对设置。长时间运行时建议使用原生 Linux SSH，避免 WSL 互操作会话生命周期影响连接。

### 后端环境、权重与数据库

以下安装命令在**服务器**运行，先将这份仓库放到服务器上的一个源码目录。实际运行的 worker 源码由控制器自动按签名部署，环境、权重和数据库需事先准备。不要在已经用于活动任务的环境中升级依赖。

```bash
# 当前目录是服务器上的本仓库源码；选择所有执行节点共享、由自己拥有的目录。
ANTIGEN_RUNTIME="$HOME/antigen-runtime"
mkdir -p "$ANTIGEN_RUNTIME/envs" "$ANTIGEN_RUNTIME/models" "$ANTIGEN_RUNTIME/databases" "$ANTIGEN_RUNTIME/work"
python3.12 -m venv "$ANTIGEN_RUNTIME/envs/controller"
"$ANTIGEN_RUNTIME/envs/controller/bin/python" -m pip install .
python3.12 -m venv "$ANTIGEN_RUNTIME/envs/foundry"
python3.12 -m venv "$ANTIGEN_RUNTIME/envs/msa"
python3.12 -m venv "$ANTIGEN_RUNTIME/envs/protenix"
```

环境分离是为了避免模型依赖冲突。适配器基于 `rc-foundry 0.2.0`、`colabfold 1.6.2`、`protenix 2.0.0` 的接口；这些是部署起点，不是本次开源版本的 GPU 实测结果。不要直接升级到 Foundry 1.x，它改变了 Python 命名空间。[Foundry 0.2.0 元数据](https://pypi.org/project/rc-foundry/0.2.0/)、[Foundry 当前迁移说明](https://github.com/RosettaCommons/foundry)

```bash
# GPU 环境先根据服务器驱动安装兼容的 PyTorch/CUDA；此处固定 Python 包版本。
"$ANTIGEN_RUNTIME/envs/foundry/bin/python" -m pip install 'torch==2.7.1' 'rc-foundry[rfd3]==0.2.0'
"$ANTIGEN_RUNTIME/envs/foundry/bin/python" -m pip install .
"$ANTIGEN_RUNTIME/envs/msa/bin/python" -m pip install 'colabfold==1.6.2'
"$ANTIGEN_RUNTIME/envs/msa/bin/python" -m pip install .
"$ANTIGEN_RUNTIME/envs/protenix/bin/python" -m pip install 'torch==2.7.1' 'protenix==2.0.0'
"$ANTIGEN_RUNTIME/envs/protenix/bin/python" -m pip install .
```

完成以下后端准备，然后再做预检：

1. **Foundry**：用该环境的 `foundry list-available` 查看模型登记名，按 [Foundry 文档](https://github.com/RosettaCommons/foundry) 安装所需 RFD3/ProteinMPNN 权重到独立目录，把目录写入 `foundry_weights`。`rfd3.checkpoint` 和 `mpnn.checkpoint` 可在科学配置里设为明确的权重文件绝对路径。仅做 Folding 时无需 Foundry。
2. **MSA**：安装适合 CPU 架构的 [MMseqs2](https://github.com/soedinglab/MMseqs2)，将可执行文件路径写入 `msa.mmseqs`。保留与你安装的 Protenix 版本对应的 `scripts/colabfold_msa.py`，写入 `msa.script`；不要只提供 `colabfold_search`，还需要配对格式转换。安装 UniRef30 和 ColabFold 环境数据库，按实际文件前缀设置 `db1`/`db3`，记录 `database_version`。数据库安装步骤见 [官方 ColabFold 兼容流程](https://github.com/bytedance/Protenix/blob/main/docs/colabfold_compatible_msa.md)。完整数据库可能需要大量磁盘与内存，模板资源值应按站点容量调整；CPU 小型工作台不应直接照搬全库申请。
3. **Protenix v2**：将自行取得授权的 `protenix-v2.pt` 放入 `folding.weights` 指向的目录。按 [上游安装/数据文档](https://github.com/bytedance/Protenix/blob/main/docs/training_inference_instructions.md) 准备 CCD 等辅助文件；`folding.data_root` 会作为 `PROTENIX_ROOT_DIR`。预检会检查 `ccd_components_file`、`ccd_components_rdkit_mol_file`、`pdb_cluster_file`、`obsolete_release_data_csv` 对应文件。这里只安装代码，不提供权重下载地址或分发权重。
4. **共享路径**：每个执行节点能读取同一路径的环境和依赖，且只在项目拥有的工作/缓存目录写入。若站点间不共享存储，使用 [高级部署](docs/advanced-deployment.md) 的显式传输配置，不把不同文件系统当作共享盘。

可用如下命令检查接口导入；这不代表模型推理已经成功：

```bash
"$ANTIGEN_RUNTIME/envs/foundry/bin/python" -c 'from rfd3.engine import RFD3InferenceConfig, RFD3InferenceEngine; from mpnn.inference_engines.mpnn import MPNNInferenceEngine'
"$ANTIGEN_RUNTIME/envs/msa/bin/colabfold_search" --help
"$ANTIGEN_RUNTIME/envs/protenix/bin/python" -c 'import runner.inference; from configs.configs_model_type import model_configs; assert "protenix-v2" in model_configs'
```

### 填写服务器配置

回到**本地仓库**：

```bash
cp configs/servers/ssh-slurm-template.yaml configs/servers/my-cluster.local.yaml
```

编辑副本，把全部 `/CONFIGURE/...` 改成真实的服务器绝对路径，修改 partition/account/qos/CPU/内存/GPU 申请。YAML 中不展开 `$HOME`、环境变量或 `~` 远端路径；填写展开后的值。服务器路径暂不支持空格或 shell 特殊字符。

然后编辑 `configs/webapp.yaml`：

```yaml
server_profile: servers/my-cluster.local.yaml
# 其余配置保留 example 中的值，再按需要修改。
```

在本机做不提交计算作业的检查：

```bash
.venv/bin/antigen-design validate projects/1ubq/configs/ubiquitin.yaml --server configs/servers/my-cluster.local.yaml
.venv/bin/antigen-design doctor projects/1ubq/configs/ubiquitin.yaml --server configs/servers/my-cluster.local.yaml --remote --report projects/1ubq/evidence/doctor.json
```

`validate` 只验证配置/映射；`doctor --remote` 会连接服务器、部署小型源码快照并检查依赖，不提交 Slurm 作业。只有 `ready: true` 才说明预检通过，仍不是 GPU 推理验收。可用 `--only msa`、`--only fold` 等分阶段诊断。

重启网页：

```bash
.venv/bin/antigen-web --config configs/webapp.yaml
```

在 Folding 加载 1UBQ 序列，或在 Design 加载 1UBQ 结构并选择区域，点击检查，再明确提交。**网页的提交按钮会消耗你配置的真实后端资源。** 后续 MSA、预测和评估自动衔接，运行与结果保存在 `projects/webapp/`。

## 3. 配置优先级与参数

| 文件 | 作用 | 路径规则 |
| --- | --- | --- |
| `configs/webapp.yaml` | 端口、项目目录、只读模式、后端 profile、资源文件、默认科学参数 | 相对于此 YAML 所在目录 |
| `configs/servers/*.local.yaml` | SSH 别名、执行环境、权重、数据库、Slurm 资源 | 后端路径使用绝对路径 |
| `projects/<id>/configs/*.yaml` | CLI 输入输出、区域、数量、种子、模型参数 | input/output 相对于科学 YAML |
| `configs/gpu-targets.local.yaml`（可选） | 多个 GPU 站点与明确的环境路径 | 服务器绝对路径 |
| `configs/web-resources.local.json` | 网页资源启停与排队策略 | 网页持久化，已运行任务继续 |
| `configs/web-resource-limits.local.json` | 网页全局并发限制 | 网页持久化，独立于科学身份 |

CLI 顺序：内置默认值 → 服务器 YAML → 科学 YAML。网页顺序：内置默认值 → 服务器 YAML → `scientific_defaults` → 表单明确提交值。表单默认值从配置加载；每次提交保存最终 JSON。`max_gpu_jobs: 0` 表示无限制，默认是 1；MSA 并发至少为 1。

网页配置的 `max_*` 和 `queue_grace_seconds` 仅初始化 JSON 策略；文件已存在时以保存后的 JSON 为准。更换资源目录前应停止控制器并检查已有任务，必要时备份旧策略后按新目录重新生成；不改动旧计划和收据。

核心科学字段：`selection.regions`、`selection.region_sets`、`numbering`、`seed`、`rfd3.enabled/samples/checkpoint/options`、`mpnn.sequences/temperature/checkpoint/options`、`msa.batch_size/threads/cache_root/gpu`、`folding.seeds/samples/options`。完整 schema 和边界见 [配置说明](docs/configuration.md)。模型/数据库/参数改变后必须使用新的输出目录。

## 4. CLI 与本机推理

无需模型的映射检查：

```bash
.venv/bin/antigen-design validate projects/1ubq/configs/ubiquitin-matrix.yaml
cp projects/1ubq/configs/ubiquitin.yaml projects/1ubq/configs/check.local.yaml
# 将 check.local.yaml 的 output 改成 ../runs/check-dry-run
.venv/bin/antigen-design run projects/1ubq/configs/check.local.yaml --dry-run
```

dry-run 写计划但不做模型计算，不可把它的目录续跑成真实任务。

本机计算复制 `configs/servers/local-template.yaml` 为 `local.local.yaml`，填写本机依赖路径，再执行 `doctor`（不加 `--remote`）。CLI 的 `run --server configs/servers/local.local.yaml --wait` 在本机启动阶段进程；需本机具备相应 GPU、驱动、模型和数据库。集群内 native Slurm 模式从 Slurm 模板改为 `transport: native`，同一站点执行时主机字段可留空。

远程分阶段真实运行示例，执行前为科学配置选择一个全新的 output：

```bash
# 以下命令会提交真实作业；示例 output 为 ../runs/ubiquitin
.venv/bin/antigen-design run projects/1ubq/configs/ubiquitin.yaml --server configs/servers/my-cluster.local.yaml --until msa --wait
.venv/bin/antigen-design resume projects/1ubq/runs/ubiquitin --only fold --wait
.venv/bin/antigen-design resume projects/1ubq/runs/ubiquitin --only metrics --wait
.venv/bin/antigen-design report projects/1ubq/runs/ubiquitin
.venv/bin/antigen-design collect projects/1ubq/runs/ubiquitin
```

`status DIRECTORY` 只读本地快照；`resume` 可能提交新任务；`collect` 取回并验证远端产物。筛选策略通过 `report DIRECTORY --policy POLICY.json` 明确选择。外部预测通过 `import-results MANIFEST.json OUTPUT_DIRECTORY` 导入并重算指标，见 [工作流与恢复](docs/workflow.md)。

## 5. 持久运行、升级与排障

在 Linux 使用 [systemd 用户服务模板](deploy/antigen-web.service.example)，把工作目录、Python 和配置路径替换成部署位置后放入 `~/.config/systemd/user/antigen-web.service`：

```bash
systemctl --user daemon-reload
systemctl --user enable --now antigen-web
journalctl --user -u antigen-web -f
```

WSL 未启用 systemd 时可在持久 WSL 终端运行，或按系统文档启用。电脑休眠/关机后远端 Slurm 作业可能继续；恢复服务会依据已有身份核对任务。停止网页并不等于取消计算，需要取消时使用对应任务的取消按钮。只允许一个可写网页实例管理同一项目，`--read-only` 可启动只读检查实例。

在另一台机器查看部署主机的网页时使用 SSH 隧道，远端 HTTP 端口保持 loopback：

```bash
ssh -N -L 8776:127.0.0.1:8776 controller-host
```

不要直接对公网开放该单用户 HTTP 服务。更新代码前处理或等待现有任务完成，保存依赖版本，更新后再运行测试与预检。旧计划绑定源码/输入/环境身份，不应修改 hash 来绕过恢复保护。

| 现象 | 检查 |
| --- | --- |
| `fcntl` 不可用 | 正在使用 Windows 原生 Python；改用 WSL/Linux |
| 网页提示未配置后端 | `server_profile` 是否设置，路径是否相对于 webapp.yaml |
| SSH 失败 | 在运行控制器的同一环境测试 BatchMode 登录，检查 host key/agent/SSH 路径 |
| `sbatch` 找不到 | 后端登录 shell 的 PATH 和 Slurm 安装 |
| `doctor` 缺少模块/文件 | 检查对应阶段 Python、权重及数据库，不能靠切换模型绕过 |
| `Source/input identity changed` | 保留旧记录，使用新配置和新 output；不要改计划 |
| `submitting` 状态不明 | 核对作业身份后显式 reconcile；不盲目重复提交 |
| CPU MSA 内存不足 | 调整新运行的资源与并发；检查数据库版本/索引，不自动转换数据库 |
| 改 YAML 后界面没变化 | 重启服务；资源并发的已保存 JSON 优先于初始化值 |

## 6. 开发、隐私与验证

```bash
.venv/bin/python -m pytest -q
node --test tests/web_ui.test.cjs       # Node 20+，无 npm 安装步骤
.venv/bin/python scripts/check_public_tree.py
.venv/bin/python scripts/check_docs.py
.venv/bin/python -m pip install -e '.[build]'
.venv/bin/python -m build
```

本仓库不包含既有用户的 SSH 配置、私钥、账户目录、IP 清单、运行档案、内部日志或其他项目样本。`.gitignore` 默认排除私有部署配置和所有用户项目，仅白名单保留公开 1UBQ 基准。发布前检查 `git diff --cached` 和隐私扫描；不要用 `git add -f` 把运行结果或权重加入仓库。扫描是静态检查，不能替代对新增内容的审阅。

本次可复核的验证边界记录在 [验证说明](docs/verification.md)。软件测试、dry-run、调度接收、真实模型完成和产物验收分别记录，不能相互替代。新增站点需单独做真实 1UBQ 验收。

Agent 请先阅读 [AGENTS.md](AGENTS.md)。其他文档：[工作流](docs/workflow.md)、[高级路由](docs/advanced-deployment.md)、[隐私与服务边界](SECURITY.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。
