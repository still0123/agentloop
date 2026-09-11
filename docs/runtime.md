# 执行控制与工具组合

Agent 核心管理模型调用、工具执行、上下文和任务退出。应用提供目标、可用能力、权限策略和结果验收，具体调查步骤由模型结合观察结果决定。

## 轮数、完成与收尾

- `max_turns` 是模型决策轮数上限。最后一轮返回的工具调用仍执行并保存结果；下一次模型调用被阻止。`Stop` Hook 可以要求继续，但不能绕过上限。
- `is_complete()` 表示调用方已接受任务结果；`should_stop()` 表示用户取消。二者分别返回 `done`、`cancelled`，提交成功不会再消耗一轮模型调用。同一批剩余工具不会执行，历史中会记录对应原因。
- `finalize_turns` 预留收尾轮次，默认 `0`。进入收尾后，只向模型提供 `finalize_tools`；请求其他工具也会被执行层拒绝。保留两轮可以让模型在报告校验失败后修正一次。未成功通过验收则继续受轮数上限约束。
- `max_seconds` 与 `finalize_seconds` 提供可选的时间预算。它们在循环和工具边界检查，**不能抢占一个正在阻塞的外部 handler**。模型客户端、工具执行器仍需配置自己的超时与取消回调；要求硬截止的服务应保留外层进程超时。

```python
agent = Agent(
    client,
    toolbox,
    hooks,
    compactor,
    system_prompt,
    max_turns=20,
    finalize_turns=2,
    finalize_tools=("submit_report",),
    is_complete=lambda: bool(accepted_report),
)
```

调用方先注册 `submit_report`，由其 handler 校验报告，成功后更新 `accepted_report`。核心不规定报告的业务字段，也不把调用某个工具视为业务目标已经达成。

## 复用通用工具

```python
from pathlib import Path
from agentloop.tools import Toolbox, build_toolbox

general, todos = build_toolbox(
    Path("./session"),
    include=("read_file", "search_file", "glob", "todo_write"),
    read_roots={"source": Path("./reference-source")},
)
application = Toolbox()
application.extend(general)
# application.add("submit_report", description, schema, handler)
```

模型用 `root="source"` 读取调用方指定的目录。`workspace` 始终指向会话目录。新增只读根目录不会扩大文件写工具的范围，路径解析会检查 `..` 与符号链接越界。`glob` 不递归进入符号链接目录，结果和遍历量有上限。

`select()` 创建工具子集；`extend()` 组合能力，遇到重名整体拒绝。应用不必复制文件搜索或计划工具的实现。工具选择只限制这些已注册 handler；一旦同时开放 Shell，Shell 自身仍需要独立的执行权限与隔离策略。

## 带来源的值引用

需要把长请求号、资源号交给模型使用时，可以先用 `ScopedReferences`
登记经过调用方核验的值，再把 `I1` 等短编号交给模型。引用表按
`(scope, kind, value)` 去重，保留来源和可选父引用；按任务范围和类型解析，
不会从普通文本自动推断归属，也不会因为对话压缩而重新生成编号。

```python
from agentloop.references import ScopedReferences

references = ScopedReferences()
ref = references.add("task-a", "request", "request-12345678", "evidence-1")
actual_value = references.resolve("task-a", ref.id, {"request"}).value
```

哪些内容可以登记、如何证明新引用与原任务相关，仍由证据采集层决定。
引用表提供的 `catalog` 可由调用方持久化；它本身不提供跨进程自动恢复。

## 命令输出与回读

Shell 的 stdout、stderr 分开保存到 `.task_outputs/commands/` 下权限为 `0600` 的文件，模型收到退出码、短预览和相对路径。长输出中间的内容可以通过 `search_file` 定位，再用 `read_file` 分页读取。

每路输出最多保存 16 MiB，超出后终止命令并明确标记为部分结果。超时或取消同样保留已读取的输出，不能据此声称检查了全部日志。命令执行会终止对应进程组，不影响其他会话。原文归档可能包含敏感内容，应由调用方管理目录访问和清理周期。

运行时还在本次请求内保存最多 128 组工具参数及结果的哈希。相同参数、相同结果重复三次时提示重新判断；不同分页或变化的结果不会被视为相同观察。它不会阻止调用或替代业务进度验收，也不判断语义相近的两次查询是否重复。

相关回归：`tests/test_execution_control.py`、`tests/test_command_capture.py`。这些测试验证执行机制，不代表真实模型任务成功率或业务诊断质量。


## 可恢复的工具输出与动态上下文

工具可以返回 `ToolOutput`，其文本照常发送给模型，原始文件路径、SHA-256 和字符范围作为内部元数据保留在消息历史中。`read_artifact(workdir, path, offset, limit)` 返回有界字符页及 `next_char_offset`，能够连续读取很长的单行 JSON。压缩再次淘汰这一页时，复用原始引用，不把带展示格式的回读文本再存成新证据。引用文件发生变化时会明确报错。内部元数据不会作为模型协议字段发送。

`read_file` 对 `.task_outputs/tool-results` 和 `.transcripts` 中的归档使用 `char_offset`、`max_chars`；普通源码文件仍使用行号 `offset`、`limit`。模型摘要只读取被观察过的字符范围，不把未读部分混入摘要。

配置 `RequestBudget` 后，默认以完整请求估算预算控制上下文，不再叠加隐式的 50,000 字符和 50 条消息阈值；调用方仍可显式设置这些限制。未配置请求预算时保留原有字符与消息数保护。估算器默认使用保守的 UTF-8 字节近似，不能当作提供商实际 token 数。

预算足够时，较旧工具结果也保持原样；预算不足时从较早的已读结果开始释放空间，保护最新工具批次。需要摘要时，按预算选择近期完整调用组，并保持工具调用和结果配对。

`Agent(..., context_provider=...)` 可在每轮调用前提供最新、程序维护的任务状态。该状态与当前工具列表重新组装到系统上下文，并纳入同一请求预算，不追加进历史、也不由模型摘要重新生成。调用方应返回有界的执行事实，明确标出被省略的记录；来自日志的内容始终只是数据。

`context_prepared` 事件记录触发原因（字符、估算 token、消息数）、淘汰结果数、复用引用数、摘要调用数以及准备后的预算情况。这些指标用于发现重复压缩和信息回读问题，不代表任务质量验收通过。

### Request controls

`RoundControl` holds an immutable set of the tools offered for a model request.
A client wrapper that narrows tools publishes that snapshot as `round_control`;
the execution loop checks it in addition to application hooks. Without a wrapper,
the loop uses its own active tool definitions. Hooks can provide a more specific
rejection reason, but cannot authorize a tool excluded by the snapshot.

`CallBudget` provides an absolute monotonic deadline for cooperative model
transports. It does not itself interrupt arbitrary blocking clients. Applications
must connect `expired()` to their transport's cancellation callback and decide
whether an interrupted call is retried or ends the task. Incomplete generated
arguments are not evidence of tool execution.

Repeated-tool reminders count consecutive identical inputs and outputs. A later
recall after other work remains a valid operation; application evidence stores
should reuse the original content identity and keep recall separate from new
query coverage.
