# 安装与部署

Nico Agent 通过 GitHub Release 安装器同时部署 Docker Compose 服务栈和本地
`nico` HTTP CLI。普通分支变更不会发布安装资产；第一次版本 Tag 成功完成后，
下面的稳定地址才会存在。

## 环境要求

- Linux、macOS，或 Windows 上的 WSL2；
- Docker Engine 或 Docker Desktop，以及 Docker Compose 2.24.4 或更高版本；
- Python 3.11 或更高版本；
- `bash` 和 `curl`；
- 当前用户有权访问 Docker daemon，无需把 Docker Socket 改成全局可写。

安装器会检查依赖和 Docker daemon，但不会申请 root 权限、自动安装 Docker，
也不会修改 Docker daemon 配置。

## 在 Tag 前本地验证完整安装链路

仓库根目录的 Makefile 可以生成与 GitHub Release 相同结构的安装资产，并用
同一个 `install.sh` 完成安装。安装阶段会使用本地版本化镜像，并把拉取策略
设为 `never`。本地服务使用独立的 `nico-agent-local-release` Compose 项目和
端口，避免复用源码开发栈的数据卷：

```bash
make release
make install
```

`make release` 会完成以下工作：

1. 构建 `nico-agent-backend:<tag>`、`nico-agent-hermes:<tag>` 和
   `nico-agent-web:<tag>`；
2. 从当前源码构建真实 CLI wheel；
3. 调用 `scripts/package-release.sh` 生成 `dist/release/install.sh`、bundle、
   `version.txt` 和 `SHA256SUMS`。

`make install` 会检查这四个资产、内部版本和三个本地镜像。它们缺失或不完整
时先自动运行 `make release`，否则直接复用，然后调用：

```bash
dist/release/install.sh \
  --version v0.2.0 \
  --bundle dist/release/nico-agent-bundle.tar.gz \
  --local-images
```

本地安装的 API 默认为 <http://localhost:28000>，Web Console 默认为
<http://localhost:28080>。PostgreSQL、Redis 和 MinIO 也分别使用独立的
`25432`、`26379`、`29010/29011` 端口。

因此可以直接从干净 checkout 执行：

```bash
make install
```

只验证本地打包和 CLI 安装而不启动服务：

```bash
make install \
  NICO_HOME=/tmp/nico-test \
  NICO_BIN_DIR=/tmp/nico-test/bin \
  INSTALL_ARGS="--no-start --non-interactive"
```

测试 Hermes 安装路径时，使用与远程安装相同的 Provider 规则：

```bash
OPENROUTER_API_KEY='<由 Secret Store 注入的值>' \
  make install RUNTIME=hermes PROVIDER=openrouter
```

`make release` 只是本地构建，不会创建 Git Tag、GitHub Release 或上传镜像。
正式发布仍只由 `v*` Tag 触发。

完成本地演练后，可以停止 Release 服务并删除程序文件：

```bash
make uninstall
```

该命令会删除版本目录、服务命令和 `nico`/`nico-service` 链接，并默认保留
Docker 数据卷、`~/.nico/config` 中与数据卷绑定的数据库/Provider 凭据，以及
`~/.nico/state` 中的本地状态。这样再次执行 `make install` 时会继续使用原密码，
不会让已有 PostgreSQL 数据卷与新生成的配置失配。需要删除全部数据时，应先
执行 `nico-service purge --yes`，卸载后再删除剩余的 `~/.nico`。

## 安装最新版本

```bash
curl -fsSL https://github.com/Phlosy/Nico-agent/releases/latest/download/install.sh | bash
```

如果希望先检查脚本再运行：

```bash
curl -fsSLO https://github.com/Phlosy/Nico-agent/releases/latest/download/install.sh
less install.sh
bash install.sh
```

默认安装使用 Nico Native。安装器会：

1. 从最新 Release 解析不可变版本 Tag；
2. 下载 bundle 和 `SHA256SUMS`，校验后才解压；
3. 把版本化部署文件放在 `~/.nico/releases/<tag>`；
4. 在 `~/.nico/config/deployment.env` 生成权限为 `0600` 的共享配置；
5. 把 Python 包安装到 `~/.nico/releases/<tag>/venv`；
6. 将 `nico` 和 `nico-service` 链接到 `~/.local/bin`；
7. 启动服务、等待 API/Console 就绪，创建本地 Tenant/Project 并配置 `local` Profile；
8. 输出 `nico setup`，由操作者在安装完成后单独配置 Native Provider。

如果 `~/.local/bin` 不在 `PATH` 中，请按安装器提示加入 shell 配置。安装完成
后运行安装器输出的下一步：

```bash
nico setup
```

安装阶段不会读取 Native Provider Key，也不会在真实 completion 验证完成前宣称
可以聊天。需要保留原来的无凭据 Mock 演示时，显式传入 `--demo`。

## 固定版本与自动化参数

使用同一 Tag 下的不可变安装器和 bundle 安装指定版本：

```bash
curl -fsSL https://github.com/Phlosy/Nico-agent/releases/download/v0.2.0/install.sh \
  | bash -s -- --version v0.2.0
```

只安装文件与 CLI，不启动服务：

```bash
bash install.sh --version v0.2.0 --no-start
```

自动化环境可以使用 `--non-interactive`，并通过 `--dir` 与 `--bin-dir`
改变数据目录和命令链接目录。运行 `bash install.sh --help` 查看完整参数。
`--local-images` 专用于带明确 `--version` 和 `--bundle` 的本地 Release 演练，
普通用户不需要手工传入它。

## 使用 Hermes Runtime

Hermes 是可选 Adapter，不是 Nico Native 的依赖。通过同一个安装入口选择它：

```bash
curl -fsSL https://github.com/Phlosy/Nico-agent/releases/latest/download/install.sh \
  | bash -s -- --runtime hermes --provider openrouter
```

`openrouter`、`openai` 和 `anthropic` 是安装器支持的 Provider 快捷名。凭据只
从对应环境变量或隐藏交互输入读取，不支持 Secret 命令行参数。非交互示例：

```bash
export OPENROUTER_API_KEY='由 Secret Store 注入的值'
bash install.sh --runtime hermes --provider openrouter --non-interactive
unset OPENROUTER_API_KEY
```

凭据写入私有部署配置，并且 Compose 只把它显式传给 Hermes Worker。已有
AgentVersion 仍需选择 `runtime_provider=hermes` 并声明 Provider/模型配置。
Hermes `0.18.2` 的 Adapter 合同已验证，但带真实凭据的外部推理和模型质量
仍需部署方验收。

## 操作服务与 CLI

```bash
nico-service status
nico-service doctor
nico-service logs
nico-service logs worker
nico-service restart
nico-service down
nico-service up
```

`down` 会保留 PostgreSQL、Redis、MinIO 和 Hermes 状态卷。`purge` 会永久
删除 Compose 命名卷，应只在明确不需要本地数据时使用：

```bash
nico-service purge --yes
```

CLI 是远程 HTTP 客户端，服务端和 CLI 虽由同一脚本安装，但不是同一个进程：

```bash
nico doctor
nico setup
nico provider list
nico agent list
nico chat
nico project new launch --goal "验证安装" --lead nico-assistant --yes
nico project open launch
```

`nico chat` 创建独立 Session，不要求 Project。`nico project` 是另一种入口，用于
有 Lead、成员 Session、任务同步和受审计指导的共享工作。安装版 CLI 与服务端
使用同一个 release bundle；可用以下命令验证完整接口：

```bash
nico project status launch
nico project timeline launch
nico project sync launch
nico project cycles launch
```

`nico setup`/`nico provider add` 的本地 Key 路径只适用于 Native Runtime。安装器
创建 `~/.nico/config/model-secrets.env`（`0600`），CLI 通过经过路径与权限校验的
`nico-service` stdin 通道暂存 Key，只重建 Native Worker；API、Web 和 Hermes
Worker 不接收该文件。验证或激活失败会回滚本次变量并恢复 Worker。

完整命令说明见 [Nico CLI](cli.md)。

## 升级与切换 Runtime

重新运行安装器即可升级到最新版本；私有配置、已有 Secret 和 Docker 命名卷
会被保留，当前 Runtime 选择也会保留；镜像引用、版本目录、CLI 环境和
`current` 指针会更新：

```bash
curl -fsSL https://github.com/Phlosy/Nico-agent/releases/latest/download/install.sh | bash
```

要切换 Runtime，重新运行安装器并传入 `--runtime native` 或
`--runtime hermes`。`nico-service` 启动所选 Profile 前会停止另一个 Worker，
避免两个 Provider 集合竞争同一队列。

## 移除

从仓库执行的本地 Release 安装可以直接运行 `make uninstall`。它会先停止服务，
验证安装目录标记，再移除程序文件；不会删除源码开发栈、Docker 数据卷，或
重新连接这些数据卷所需的 `config`/`state`。该命令可以重复运行。

手工移除或通过远程 Release 安装时，保留数据卷并移除程序文件：

```bash
nico-service down
rm -f ~/.local/bin/nico ~/.local/bin/nico-service
rm -f ~/.nico/current
rm -rf ~/.nico/bin ~/.nico/releases
```

不要在保留数据卷时删除 `~/.nico/config/deployment.env`：PostgreSQL 初始化密码
存储在数据卷中，重新生成配置会导致 API 无法认证。新版 `nico-service up` 会在
API 启动前校验这组凭据，并无损修复由旧版卸载流程造成的历史密码失配。

如果连本地服务数据也不保留，应先执行 `nico-service purge --yes`，再删除整个
`~/.nico`。
自定义过 `--dir` 或 `--bin-dir` 时，需要替换上面的路径。安装器不会删除
Docker Engine、共享镜像缓存，或用户自行配置的外部 Secret。

## 从源码开发

贡献者仍可从 checkout 构建本地镜像并以可编辑方式安装 CLI：

```bash
cp .env.example .env
scripts/dev.sh --detach
python3 -m venv .venv
.venv/bin/pip install -e 'backend[dev]'
```

源码部署使用根目录 `.env`，Release 安装则使用
`~/.nico/config/deployment.env`；不要把任一实际 Secret 文件提交到 Git。

## 发布边界

面向 `main` 的 Pull Request 和进入 `main` 的 push 只运行 Test workflow。
只有与包版本一致的 `vX.Y.Z` Tag 才触发 Release workflow；它复用相同测试门，
随后发布版本化的 backend、Hermes 和 web GHCR 镜像，再创建带校验和的 GitHub
Release。首次公开发布前，维护者还必须确认三个 GHCR Package 允许匿名拉取。

这仍是 Alpha 的本机/受信网络部署路径。它没有增加生产身份认证、备份、
Secret Manager、镜像签名、Kubernetes Manifest 或公网加固。
