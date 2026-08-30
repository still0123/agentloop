"""模型接入层 —— 多提供商路由 + 统一内部格式。

这是 AgentLoop 相对教程版的主要差异化点之一：教程绑定 Anthropic SDK
单一模型；AgentLoop 把"调用模型"抽象成边界适配器：

    内部统一格式（Anthropic 风格 content blocks）
        ↑↓ 适配只发生在这层边界上
    OpenAI 兼容线（GLM / DeepSeek / Qwen / Moonshot / Ollama / vLLM / GPT）
    Anthropic 原生线
    MockClient（脚本回放，测试零网络）
    FallbackClient（主模型连续失败自动切换备用模型）

为什么内部格式选 Anthropic 风格的 blocks：
    messages 为 str | list[block]，tool_use / tool_result 是显式的块类型，
    与 learn-claude-code 课程一一对应（学习迁移成本最低），
    且天然支持一条消息里混合"文本 + 多个工具调用"。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field


@dataclass
class ModelResponse:
    """所有适配器统一返回的结构。blocks 为内部 content blocks 格式。"""

    text: str = ""
    blocks: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # {"input_tokens": int, "output_tokens": int}


class ModelError(RuntimeError):
    pass


class RetryableModelError(ModelError):
    pass


# ---------------------------------------------------------------------------
# 内部格式 → OpenAI 线格式的转换
# ---------------------------------------------------------------------------

def _openai_wire_messages(system: str, messages: list) -> list:
    wire = [{"role": "system", "content": system}]
    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            wire.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            entry = {"role": "assistant", "content": text}
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                    },
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            if calls:
                entry["tool_calls"] = calls
            wire.append(entry)
        else:
            # user 消息携带 tool_result blocks：逐条展开为 role=tool 消息，
            # 必须紧跟在带 tool_calls 的 assistant 消息之后（协议配对要求）。
            for b in content:
                if b.get("type") == "tool_result":
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": _stringify(b.get("content")),
                        }
                    )
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            if joined:
                wire.append({"role": "user", "content": joined})
    return wire


def _stringify(content) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


class OpenAICompatClient:
    """任何 OpenAI 兼容 chat/completions 端点（GLM/DeepSeek/Qwen/Ollama/vLLM…）。"""

    adapter = "openai-compat"

    def __init__(self, model: str, base_url: str, api_key: str,
                 max_tokens: int = 8000, timeout: float = 120.0, retries: int = 3) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries

    def complete(self, system: str, messages: list, tools: list) -> ModelResponse:
        import httpx  # 延迟导入：Mock 路径的测试环境无需安装

        payload = {
            "model": self.model,
            "messages": _openai_wire_messages(system, messages),
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error = None
        for attempt in range(self.retries):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=headers, timeout=self.timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 529):
                    raise RetryableModelError(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    # 4xx（除 429）重试没有意义，直接失败（可能触发 Fallback）
                    raise ModelError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return self._parse(resp.json())
            except (RetryableModelError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise ModelError(f"{self.model}: {self.retries} 次重试后仍失败: {last_error}")

    @staticmethod
    def _parse(data: dict) -> ModelResponse:
        message = data["choices"][0]["message"]
        blocks = []
        if message.get("content"):
            blocks.append({"type": "text", "text": message["content"]})
        for call in message.get("tool_calls") or []:
            raw_args = call["function"].get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args)
                if not isinstance(parsed, dict):
                    parsed = {"_raw": raw_args}
            except json.JSONDecodeError:
                parsed = {"_raw": raw_args}
            blocks.append(
                {"type": "tool_use", "id": call["id"],
                 "name": call["function"]["name"], "input": parsed}
            )
        usage = data.get("usage") or {}
        return ModelResponse(
            text=message.get("content") or "",
            blocks=blocks,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )


class AnthropicClient:
    """Anthropic 原生 /v1/messages。内部格式即线格式，几乎透传。"""

    adapter = "anthropic"

    def __init__(self, model: str, api_key: str, base_url: str = "https://api.anthropic.com",
                 max_tokens: int = 8000, timeout: float = 120.0, retries: int = 3) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries

    def complete(self, system: str, messages: list, tools: list) -> ModelResponse:
        import httpx

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            # json 往返一趟：剥掉测试里可能混入的非序列化对象
            "messages": json.loads(json.dumps(messages, ensure_ascii=False, default=str)),
        }
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {"type": "object"}),
                }
                for t in tools
            ]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        last_error = None
        for attempt in range(self.retries):
            try:
                resp = httpx.post(
                    f"{self.base_url}/v1/messages",
                    json=payload, headers=headers, timeout=self.timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 529):
                    raise RetryableModelError(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise ModelError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return self._parse(resp.json())
            except (RetryableModelError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise ModelError(f"{self.model}: {self.retries} 次重试后仍失败: {last_error}")

    @staticmethod
    def _parse(data: dict) -> ModelResponse:
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            blocks=blocks,
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
        )


class MockClient:
    """按脚本回放的假模型 —— 让整套 harness 不联网也能被完整测试。

    turns 中每个元素：
        str                      → 纯文本最终回复（循环结束）
        [(name, input), ...]     → 一批 tool_use 块（循环继续）
    """

    adapter = "mock"

    def __init__(self, turns, model: str = "mock") -> None:
        self.turns = list(turns)
        self.model = model
        self.calls: list[dict] = []  # 记录每次调用的入参，供测试断言

    def complete(self, system: str, messages: list, tools: list) -> ModelResponse:
        import copy
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": list(tools)})
        if not self.turns:
            text = "(mock script exhausted)"
            return ModelResponse(text=text, blocks=[{"type": "text", "text": text}])
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return ModelResponse(
                text=turn,
                blocks=[{"type": "text", "text": turn}],
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        blocks = [
            {"type": "tool_use", "id": f"toolu_{i:03d}", "name": name, "input": dict(args or {})}
            for i, (name, args) in enumerate(turn)
        ]
        return ModelResponse(blocks=blocks, usage={"input_tokens": 10, "output_tokens": 5})


class FallbackClient:
    """主模型抛 ModelError 时按顺序切换备用模型（可跨提供商）。"""

    def __init__(self, clients: list) -> None:
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        self.clients = clients
        self.switched_to: list[str] = []  # 记录发生过切换的位置，便于观测

    @property
    def model(self) -> str:
        return self.clients[0].model

    def complete(self, system: str, messages: list, tools: list) -> ModelResponse:
        last_error = None
        for index, client in enumerate(self.clients):
            try:
                response = client.complete(system, messages, tools)
                if index > 0:
                    self.switched_to.append(client.model)
                return response
            except ModelError as exc:
                last_error = exc
        raise last_error


# ---------------------------------------------------------------------------
# 提供商档案与路由
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str
    api_key_envs: tuple
    adapter: str


PROFILES = {
    "glm": ProviderProfile("glm", "https://open.bigmodel.cn/api/paas/v4",
                           ("GLM_API_KEY", "ZHIPU_API_KEY"), "openai"),
    "deepseek": ProviderProfile("deepseek", "https://api.deepseek.com",
                                ("DEEPSEEK_API_KEY",), "openai"),
    "qwen": ProviderProfile("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            ("DASHSCOPE_API_KEY", "QWEN_API_KEY"), "openai"),
    "moonshot": ProviderProfile("moonshot", "https://api.moonshot.cn/v1",
                                ("MOONSHOT_API_KEY",), "openai"),
    "openai": ProviderProfile("openai", "https://api.openai.com/v1",
                              ("OPENAI_API_KEY",), "openai"),
    "anthropic": ProviderProfile("anthropic", "https://api.anthropic.com",
                                 ("ANTHROPIC_API_KEY",), "anthropic"),
}

_PREFIX_RULES = (
    ("glm", "glm"), ("chatglm", "glm"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"), ("qwq", "qwen"),
    ("moonshot", "moonshot"), ("kimi", "moonshot"),
    ("claude", "anthropic"),
    ("gpt-", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
)


def detect_provider(model: str) -> str | None:
    """按模型名前缀猜提供商；猜不中返回 None（走通用 OpenAI 兼容配置）。"""
    lowered = model.lower()
    for prefix, provider in _PREFIX_RULES:
        if lowered.startswith(prefix):
            return provider
    return None


def _first_env(envs: tuple, get) -> str | None:
    for name in envs:
        value = (get(name) or "").strip()
        if value:
            return value
    return None


def build_client(model: str | None = None, env: dict | None = None):
    """根据环境变量组装模型客户端。优先级：
    显式 BASE_URL+API_KEY > AGENTLOOP_PROVIDER > 模型名前缀检测 > openai。
    """
    import os as _os
    if env is not None:  # 测试注入用
        get = env.get
    else:
        get = _os.environ.get

    model = (model or get("AGENTLOOP_MODEL", "")).strip()
    if not model:
        raise SystemExit(
            "未设置模型。请在 .env 或环境变量中配置 AGENTLOOP_MODEL，例如：\n"
            "  AGENTLOOP_MODEL=glm-4.6        GLM_API_KEY=...\n"
            "  AGENTLOOP_MODEL=deepseek-chat  DEEPSEEK_API_KEY=...\n"
            "  AGENTLOOP_MODEL=qwen-max       DASHSCOPE_API_KEY=...\n"
            "  AGENTLOOP_MODEL=claude-sonnet-4-5  ANTHROPIC_API_KEY=...\n"
            "  或自定义端点：AGENTLOOP_BASE_URL + AGENTLOOP_API_KEY"
        )

    base_url = get("AGENTLOOP_BASE_URL", "").strip()
    api_key = get("AGENTLOOP_API_KEY", "").strip()
    provider = get("AGENTLOOP_PROVIDER", "").strip().lower() or None
    max_tokens = int(get("AGENTLOOP_MAX_TOKENS", "8000"))

    if provider and provider not in PROFILES:
        raise SystemExit(f"未知 AGENTLOOP_PROVIDER: {provider}，可选: {sorted(PROFILES)}")

    if provider == "anthropic":
        key = api_key or _first_env(PROFILES["anthropic"].api_key_envs, get)
        if not key:
            raise SystemExit("缺少 ANTHROPIC_API_KEY")
        return AnthropicClient(model, key, base_url=base_url or PROFILES["anthropic"].base_url,
                               max_tokens=max_tokens)

    if base_url:  # 自定义 OpenAI 兼容端点（Ollama 等本地服务常无鉴权）
        return OpenAICompatClient(model, base_url, api_key or "EMPTY", max_tokens=max_tokens)

    profile = PROFILES[provider or detect_provider(model) or "openai"]
    key = api_key or _first_env(profile.api_key_envs, get)
    if not key:
        raise SystemExit(
            f"提供商 {profile.name} 缺少 API key，请设置 {profile.api_key_envs[0]}"
        )
    # 前缀检测也必须尊重适配器类型，否则 claude-* 会误走 OpenAI 线协议。
    if profile.adapter == "anthropic":
        return AnthropicClient(model, key, base_url=profile.base_url, max_tokens=max_tokens)
    return OpenAICompatClient(model, profile.base_url, key, max_tokens=max_tokens)
