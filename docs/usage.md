# 使用指南

AgentLoop 需要 Python 3.10 或更高版本。默认在当前目录执行文件和 Shell 工具，
请从目标项目目录启动。

## 安装

```bash
git clone https://github.com/still0123/agentloop.git
cd agentloop
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env` 并至少配置一个模型。DeepSeek 示例：

```dotenv
AGENTLOOP_MODEL=deepseek-chat
DEEPSEEK_API_KEY=sk-...
```

## 命令行与 Web

在目标工作区中执行单次任务：

```bash
python -m agentloop "列出当前目录中的 Python 文件"
```

不带参数启动连续会话：

```bash
python -m agentloop
```

启动本地 Web UI：

```bash
agentloop web
```

Web 服务仅监听 `127.0.0.1`，默认随机选择端口并打开浏览器。可指定端口，或禁止自动打开浏览器：

```bash
agentloop web --port 8787 --no-open
```

Web 会话按工作区持久化，支持创建、切换、改名和删除。运行中可以停止任务；正在执行的
`bash` 命令会终止其子进程组。

## 工具与权限

内置工具包括 `bash`、`read_file`、`search_file`、`write_file`、`edit_file`、`glob` 和
`todo_write`。文件工具限制在工作区路径内；`read_file` 支持按行分页，`search_file` 返回有界
上下文，适合从归档或日志中定位证据。

工具调用先经过权限 Hook：固定拒绝规则会直接阻止执行，命中风险 Shell 命令时会请求本地用户
确认。该策略是模式匹配，不提供生产级 Shell 沙箱隔离；只应在可信工作区和受控环境中使用。

## 模型路由与配置

模型名通常用于选择提供商和对应的密钥环境变量：

| 模型名前缀 | 提供商 | 密钥环境变量 |
|---|---|---|
| `glm`、`chatglm` | 智谱 GLM | `GLM_API_KEY` 或 `ZHIPU_API_KEY` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` |
| `qwen`、`qwq` | DashScope | `DASHSCOPE_API_KEY` 或 `QWEN_API_KEY` |
| `moonshot`、`kimi` | Moonshot | `MOONSHOT_API_KEY` |
| `claude` | Anthropic | `ANTHROPIC_API_KEY` |
| `gpt-`、`o1`、`o3`、`o4` | OpenAI | `OPENAI_API_KEY` |

也可显式指定提供商。例如使用 Anthropic：

```dotenv
AGENTLOOP_PROVIDER=anthropic
AGENTLOOP_MODEL=claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

接入兼容 OpenAI API 的本地端点时，单独使用以下配置，并将模型名替换为服务中已安装的模型：

```dotenv
AGENTLOOP_PROVIDER=openai
AGENTLOOP_MODEL=llama3
AGENTLOOP_BASE_URL=http://localhost:11434/v1
AGENTLOOP_API_KEY=ollama
```

主模型失败后的候选模型可用逗号分隔，候选提供商也需要配置对应的密钥：

```dotenv
AGENTLOOP_FALLBACK_MODELS=deepseek-chat,qwen-max
```

单个客户端对 429、部分 5xx、超时和传输错误做指数退避。重试耗尽后，
`FallbackClient` 才会依次切换候选模型。

### 上下文预算

以下配置同时用于模型单次输出和完整请求预算：

```dotenv
# 单次模型输出上限
AGENTLOOP_MAX_TOKENS=8000

# 完整请求窗口与安全余量；默认值为 64000 / 8000 / 2000。
AGENTLOOP_CONTEXT_TOKENS=64000
AGENTLOOP_CONTEXT_MARGIN=2000
```

完整请求预算覆盖 system prompt、工具定义与消息。默认使用 UTF-8 字节数作保守估算，
不是提供商的精确 tokenizer 计数。超大工具结果会保存到工作区归档，模型可使用
`search_file` 和 `read_file` 定向回读。

## macOS 桌面应用

桌面版通过 pywebview 承载同一套本地 Web UI。先建立用户配置：

```bash
mkdir -p ~/.agentloop
cp .env.example ~/.agentloop/.env
# 编辑 ~/.agentloop/.env；可选设置 AGENTLOOP_WORKDIR=/path/to/project
```

安装构建依赖并生成应用：

```bash
python -m pip install -e ".[dev,desktop]"
make macos-app
open dist/AgentLoop.app
```

生成 PKG 与 DMG：

```bash
make macos-package
ls dist/installers/
```

应用默认使用 `~/.agentloop/workspace`；`AGENTLOOP_WORKDIR` 可指定可信项目目录。构建脚本
使用本机临时签名，适合本地安装和验证。公开分发前需要使用 Developer ID 签名并完成公证。

## 开发检查

```bash
make install
make format
make lint
make test
make check
```

测试使用离线模型响应，不需要配置 API Key。CI 在 Python 3.10–3.13 上执行 Ruff 与 Pytest；
格式和静态规则定义在 `pyproject.toml`。
