# DeepContrib：开源贡献 Agent 工作台

DeepContrib 将公开 GitHub 仓库中的 Issue 转成可审查的代码贡献。用户在网页中输入仓库和 Issue 编号，Agent 分析代码并生成方案、Patch，经人工批准后运行隔离测试与审查，最后创建关联原 Issue 的 Draft PR。

当前版本面向本地单用户使用，支持 Python / pytest 项目的最小贡献流程。模型使用 Deep Agents 与 LangChain 接入，前端使用 Next.js，后端使用 FastAPI，PostgreSQL 保存任务和记忆。

## 功能与边界

- 固定仓库 base SHA，保存方案、Patch、测试报告和审查报告。
- 方案、Patch、发布分别审批；自动生成 Patch，也可以提交手动 unified diff。
- 在 Docker 中执行固定 pytest 命令，测试容器禁用网络、限制资源，不接收宿主凭据。
- 发布到任务分支，创建 Draft PR，正文自动带上 `Fixes #<Issue>`。
- 保存和编辑跨任务偏好，刷新页面可通过任务 URL 恢复状态。

目前只接受公开 github.com 仓库，单次执行一个任务。完整测试流程需要目标仓库适配提供的 Python / pytest 镜像；不会自动安装任意项目依赖。截图与原生多模态输入尚未实现。发布面向 main 分支的简单工作流，不应视为支持任意项目的通用自动修复服务。请在本机使用，当前没有多用户认证。

## 环境准备（Windows / PowerShell）

安装 Git、GitHub CLI（gh）、uv、Node.js 22 或更高版本、Docker Desktop，并启动 Docker 的 Linux 容器引擎。确保 3000、8000、5432 端口可用。

```powershell
git clone https://github.com/0Tty0/deepcontrib.git
cd deepcontrib
uv sync --project backend --python 3.11 --locked --no-editable
npm --prefix frontend ci
Copy-Item .env.example .env
gh auth login
```

Python 由 uv 管理，虚拟环境位于 `backend/.venv`；Node 依赖位于 `frontend/node_modules`。两者均不进入 Git。

## 配置模型

编辑根目录 `.env`。例如，使用支持工具调用的 DeepSeek 模型时，按服务商账户提供的模型名称和兼容地址配置：

```dotenv
DEEPCONTRIB_MODEL=openai:deepseek-chat
OPENAI_API_KEY=填入你的模型服务密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1
DEEPCONTRIB_USE_RESPONSES_API=false
DATABASE_URL=postgresql://deepcontrib:deepcontrib@localhost:5432/deepcontrib
DEEPCONTRIB_DATA_DIR=./data
DEEPCONTRIB_MAX_TASK_SECONDS=1800
DEEPCONTRIB_MAX_MODEL_CALLS=40
```

这里的 `openai:` 表示使用 OpenAI 兼容协议，不要求购买 OpenAI API。Qwen 同样可以使用此协议：将模型名换成账户可用的 Qwen 模型，将地址换成对应区域的 DashScope 兼容地址，例如 `https://dashscope.aliyuncs.com/compatible-mode/v1`，并保持 `DEEPCONTRIB_USE_RESPONSES_API=false`。模型名称、区域和工具调用支持请以自己的服务商账户为准。

`.env` 仅供本地使用，不要提交密钥。启动脚本会加载它；直接调用 CLI 时需自行将变量加载到进程环境。

## 准备隔离测试镜像

分析页面可以先启动，但要完成测试和发布，必须配置测试镜像。镜像必须使用仓库摘要 `名称@sha256:摘要`，不能只填写标签或本地镜像 ID。

项目提供 `backend/docker/test-image/Dockerfile`。以下以你有推送权限的容器镜像仓库为例（替换 YOUR_NAMESPACE）：

```powershell
docker login
$image = 'YOUR_NAMESPACE/deepcontrib-python-pytest:8.4.2'
docker build -f backend/docker/test-image/Dockerfile -t $image .
docker push $image
docker image inspect $image --format '{{index .RepoDigests 0}}'
```

将最后输出的完整引用写入 `.env`：

```dotenv
DEEPCONTRIB_TEST_IMAGE=YOUR_NAMESPACE/deepcontrib-python-pytest@sha256:实际的64位摘要
```

其他使用者可直接拉取公开镜像，或登录有权访问的私有镜像仓库后拉取。不要复制示例摘要作为实际配置。

## 启动与操作

```powershell
.\scripts\start.ps1
```

如果系统阻止本地脚本运行，可使用 `powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1`。

打开 http://127.0.0.1:3000 ，后端健康检查为 http://127.0.0.1:8000/api/v1/health 。启动脚本会启动 PostgreSQL、API 和网页，日志写入 `.deepcontrib/`。

1. 输入公开仓库 URL 和已有 Issue 编号，开始分析。
2. 检查方案与文件范围，批准方案。
3. 检查自动生成的 Patch，批准后在任务副本应用。
4. 查看测试与审查结果，按页面提示推进；失败时根据报告修改。
5. 填写 Fork 用户名、PR 标题和正文，生成发布卡。
6. 检查发布卡并批准，系统推送分支、创建 Draft PR。Fork 用户名填写 gh 当前登录用户；PR 会关联任务对应的 Issue。

首次体验建议使用自己的独立测试仓库。可将 `tests/fixtures/python-bug` 的内容放在该仓库根目录，再根据 ISSUE.md 创建 Issue；此样例用于演示重复空格处理缺陷。

### Memory 示例

在 Memory 面板填写：

```text
记忆键：response_style
记忆内容：请使用简洁的中文 Markdown，说明修复原因和验证结果。
```

点击“记住偏好”。后续分析会将已保存偏好作为上下文参考；记忆不会改变固定测试命令或跳过审批。页面可编辑、删除偏好，仓库专属记忆通过 `/api/v1/memories` 的 repository scope 管理。

### 停止

```powershell
.\scripts\stop.ps1
docker compose down
```

第一条停止 API/UI，第二条停止 PostgreSQL，保留数据库卷。`docker compose down -v` 会永久删除本项目数据库数据；本地任务产物另存于 `data/`。

## 开发与验证

```powershell
uv run --project backend --python 3.11 --no-editable --reinstall-package deepcontrib-backend pytest backend/tests -q
uv run --project backend --no-editable ruff check backend/src backend/tests
uv run --project backend --no-editable mypy backend/src/deepcontrib
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run build
cd frontend
npx playwright install chromium
npm run test:e2e
```

默认测试不使用模型密钥，不推送真实 GitHub PR。需要 Docker 或 PostgreSQL 的集成测试会按条件跳过。GitHub Actions 执行后端检查和前端构建、浏览器测试。实际模型连通性和远程发布需要自己的凭据与测试仓库。

## 目录

| 路径 | 用途 |
| --- | --- |
| backend/src/deepcontrib | Agent、API、任务状态、审批、发布和存储 |
| backend/tests | 后端测试 |
| backend/docker/test-image | 隔离测试镜像 |
| frontend/app、frontend/lib | 网页工作台 |
| frontend/tests | 浏览器流程测试 |
| scripts | Windows 启停、检查和镜像构建 |
| tests/fixtures | 可复现的 Python 缺陷样例 |
| .env.example | 无密钥的配置模板 |

## 常见问题

- 数据库连接失败：检查 Docker Desktop、`docker compose ps` 和 `.env` 的 DATABASE_URL。
- GitHub 下载或发布失败：运行 `gh auth status`，检查网络、仓库权限及 Issue 是否存在。
- 模型调用失败：检查模型名称、密钥、BASE_URL 和 Chat Completions 设置。
- 测试环境不可用：检查镜像摘要配置，确认 Docker 能拉取该镜像。
- 清空任务后页面仍显示旧任务：打开不带 `?task=...` 的首页。

本仓库不包含本地密钥、数据库、日志、视频、测试仓库副本或项目执行清单。首次 clone 后需要按上述步骤安装依赖和配置自己的外部服务。
