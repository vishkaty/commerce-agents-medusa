"""The two agents on the Claude Agent SDK, shaped like the reference's Messages API
agents so the upstream host routes (``demo_common``) can drive them unchanged.

``demo_common`` expects a ``TurnAgent``: ``stream_turn(messages, session, state)`` yielding
``AgentEvent``s, ``update_memory``, and the attributes ``config``, ``skills``, ``memory``
and ``executor_class`` (for button adds and card approvals that run the executor
directly). This module provides that over ``ClaudeSDKClient``: one client per session id,
the reference's own toolset behind it, and the toolset's tool events re-emitted after
the turn in the same event vocabulary the web apps render.

Differences from the Messages API runtime, by design of the SDK path: cards and text
arrive when the turn ends rather than mid-turn, usage carries the SDK's cost estimate
instead of token counts, and memory extraction is not run (M6 in the program plan).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)
from commerce_common.agent_sdk import (
    TurnResult,
    build_sdk_tools,
    close_on_presentation_hook,
    ensure_project_skills,
)
from commerce_common.memory import (
    InMemoryMemoryStore,
    MemoryRuntime,
    MemoryStore,
    MemoryWriteFilter,
)
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome
from merchant_agent import (
    MerchantAgentConfig,
    MerchantBackend,
    MerchantSessionContext,
    MerchantSessionState,
)
from merchant_agent.executor import MerchantToolExecutor
from merchant_agent.executor import build_memory as build_merchant_memory
from merchant_agent_sdk import agent as merchant_sdk
from merchant_agent_sdk.merchant_tools import MerchantToolset, build_merchant_server
from merchant_agent_sdk.merchant_tools import allowed_tool_names as merchant_tool_names
from shopping_agent import (
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
    StorefrontBackend,
)
from shopping_agent.executor import ShoppingToolExecutor
from shopping_agent.executor import build_memory as build_shopping_memory
from shopping_agent.serialization import cart_payload
from shopping_agent_sdk import agent as shopping_sdk
from shopping_agent_sdk import shopping_tools as shopping_sdk_tools
from shopping_agent_sdk.shopping_tools import ShoppingToolset
from shopping_agent_sdk.shopping_tools import allowed_tool_names as shopping_tool_names

from commerce_medusa.host.order_actions import LAB_TOOL_CONTRACTS, LAB_TOOL_NAMES, execute_lab_tool

logger = logging.getLogger("lab.host.sdk")

TurnRunner = Callable[[Any, str, Any], Awaitable[TurnResult]]
ClientFactory = Callable[[ClaudeAgentOptions], Any]


class CollectingShoppingToolset(ShoppingToolset):
    """The reference toolset plus every event each tool produced, in call order."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.calls: list[tuple[str, dict[str, Any], ToolOutcome]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        if name in LAB_TOOL_NAMES:  # the lab's order-action tools
            outcome = await execute_lab_tool(
                name,
                args,
                backend=self.backend,
                session=self.session,
                state=self.state,
                max_chars=getattr(self.config, "max_fenced_chars", 12000),
            )
            self.round_calls.append(name) if hasattr(self, "round_calls") else None
            self.turn_calls.append(name) if hasattr(self, "turn_calls") else None
        else:
            outcome = await super().execute(name, args)
        self.calls.append((name, dict(args), outcome))
        return outcome

    def drain_calls(self) -> list[tuple[str, dict[str, Any], ToolOutcome]]:
        calls, self.calls = list(self.calls), []
        return calls


def build_lab_shopping_server(toolset: CollectingShoppingToolset) -> McpSdkServerConfig:
    """The reference server's tools plus the lab's order-action tools."""
    contracts = {**shopping_sdk_tools.tool_contracts(toolset.config), **LAB_TOOL_CONTRACTS}
    names = [*shopping_sdk_tools.tool_names(toolset.config), *LAB_TOOL_NAMES]
    return create_sdk_mcp_server(
        name=shopping_sdk_tools.SERVER_NAME,
        version=shopping_sdk_tools.SERVER_VERSION,
        tools=build_sdk_tools(toolset, contracts, names),
    )


def lab_shopping_tool_names(config: ShoppingAgentConfig) -> list[str]:
    lab = [shopping_sdk_tools.mcp_tool_name(n) for n in LAB_TOOL_NAMES]
    return [*shopping_tool_names(config), *lab]


class CollectingMerchantToolset(MerchantToolset):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.calls: list[tuple[str, dict[str, Any], ToolOutcome]] = []

    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        outcome = await super().execute(name, args)
        self.calls.append((name, dict(args), outcome))
        return outcome

    def drain_calls(self) -> list[tuple[str, dict[str, Any], ToolOutcome]]:
        calls, self.calls = list(self.calls), []
        return calls


@dataclass
class Conversation:
    client: Any
    toolset: Any
    started_at: float


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """The text of the transcript's last user message; a list of text blocks is joined
    (the host puts an app-events note before the message that way)."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts = [
            b.get("text", "")
            for b in content or []
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n\n".join(p for p in parts if p)
    return ""


def _summary(outcome: ToolOutcome) -> str:
    text = " ".join((outcome.result_text or "").split())
    if outcome.blocked:
        return text[:300] or f"held by the {outcome.blocked} gate"
    if outcome.is_error:
        return text[:300] or "failed"
    return "ok"


def _usage(result: TurnResult, elapsed_ms: int) -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd_estimate": result.cost_usd,
        "elapsed_ms": elapsed_ms,
    }


class _SdkAgentBase:
    """What both roles share: the per-session client registry and the turn-to-events
    translation. Subclasses build options and toolsets and merge session state."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        turn_runner: TurnRunner | None = None,
        max_turns: int = 16,
        extract_memory: bool = False,
    ) -> None:
        self._client_factory = client_factory or ClaudeSDKClient
        self._turn_runner = turn_runner
        self._max_turns = max_turns
        self._extract_memory = extract_memory  # the post-turn memory pass
        self._conversations: dict[str, Conversation] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- subclass hooks ------------------------------------------------------------------

    def _build(self, session: Any) -> tuple[ClaudeAgentOptions, Any]:
        raise NotImplementedError

    def _sync_in(self, toolset: Any, state: Any) -> None:
        raise NotImplementedError

    def _sync_out(self, toolset: Any, state: Any) -> None:
        raise NotImplementedError

    async def _after_turn(self, session: Any, toolset: Any) -> list[AgentEvent]:
        return []

    def _default_runner(self) -> TurnRunner:
        raise NotImplementedError

    # -- registry --------------------------------------------------------------------------

    async def _conversation(self, session: Any) -> Conversation:
        conversation = self._conversations.get(session.session_id)
        if conversation is not None:
            return conversation
        options, toolset = self._build(session)
        client = self._client_factory(options)
        if hasattr(client, "connect"):
            await client.connect()
        conversation = Conversation(client=client, toolset=toolset, started_at=time.time())
        self._conversations[session.session_id] = conversation
        return conversation

    async def forget(self, session_id: str) -> None:
        conversation = self._conversations.pop(session_id, None)
        if conversation is not None and hasattr(conversation.client, "disconnect"):
            try:
                await conversation.client.disconnect()
            except Exception:  # a dead subprocess is already forgotten
                logger.debug("disconnect failed for %s", session_id, exc_info=True)

    async def close(self) -> None:
        for session_id in list(self._conversations):
            await self.forget(session_id)

    # -- the TurnAgent protocol ------------------------------------------------------------

    async def stream_turn(
        self, messages: list[dict[str, Any]], session: Any, state: Any
    ) -> AsyncIterator[AgentEvent]:
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            started = time.perf_counter()
            conversation = await self._conversation(session)
            toolset = conversation.toolset
            self._sync_in(toolset, state)
            toolset.session = session  # the host's clock and page context for this turn
            toolset.executor.session = session
            text = last_user_text(messages)
            runner = self._turn_runner or self._default_runner()
            streamed: list[str] = []
            try:
                if "on_text" in inspect.signature(runner).parameters:
                    # Text reaches the host as it arrives, ahead of the tool events
                    # the toolset collected, instead of once at the end.
                    queue: asyncio.Queue[str] = asyncio.Queue()
                    task = asyncio.create_task(
                        runner(conversation.client, text, toolset, on_text=queue.put_nowait)
                    )
                    while not task.done():
                        getter = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait({task, getter}, return_when="FIRST_COMPLETED")
                        if getter in done:
                            chunk = getter.result()
                            streamed.append(chunk)
                            yield AgentEvent.text_delta(chunk)
                        else:
                            getter.cancel()
                    while not queue.empty():
                        chunk = queue.get_nowait()
                        streamed.append(chunk)
                        yield AgentEvent.text_delta(chunk)
                    result = task.result()
                else:
                    result = await runner(conversation.client, text, toolset)
            except Exception as error:
                logger.exception("SDK turn failed for %s", session.session_id)
                await self.forget(session.session_id)
                yield AgentEvent.error(f"The assistant could not complete the turn: {error}")
                return
            self._sync_out(toolset, state)
            for index, (name, args, outcome) in enumerate(toolset.drain_calls()):
                call_id = f"sdk-{index}"
                yield AgentEvent.tool_call(name, call_id, args)
                for event in outcome.events:
                    yield event
                yield AgentEvent.tool_result(
                    name,
                    call_id,
                    _summary(outcome),
                    is_error=outcome.is_error,
                    status="blocked" if outcome.blocked else None,
                    reason=outcome.blocked,
                )
            for event in await self._after_turn(session, toolset):
                yield event
            if result.text and not streamed:
                yield AgentEvent.text_delta(result.text)
            for error in result.tool_errors:
                yield AgentEvent.tool_result("tool", "sdk-error", error, is_error=True)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            messages.append({"role": "assistant", "content": result.text or ""})
            yield AgentEvent.turn_complete(
                "error" if result.is_error else "end_turn",
                _usage(result, elapsed_ms),
                elapsed_ms,
                0,
            )

    async def update_memory(self, messages: list[dict[str, Any]], session: Any) -> None:
        """The post-turn memory pass on the SDK path: the last exchange, and only
        the last exchange, goes to the role's extraction prompt through a tool-less SDK
        query on the same login; the runtime's write filter and dedupe apply as on the
        Messages API path."""
        memory: MemoryRuntime | None = getattr(self, "memory", None)
        if not self._extract_memory or memory is None or not memory.enabled:
            return None
        transcript = SdkMessagesShim.transcript_of(messages)
        subject = getattr(session, "user_id", None) or getattr(session, "operator", "")
        shim = SdkMessagesShim(self._client_factory, model=getattr(self, "config", None))
        await memory.extract(shim, subject, session.session_id, transcript)
        return None


class SdkMessagesShim:
    """What the memory extractor needs of an Anthropic client (``messages.create`` and a
    response with content blocks), served by a tool-less Agent SDK query on the SDK's own
    login (no API key is read). The extractor's ``record_fact`` tool is emulated: the model
    is asked for one JSON object and its facts come back as tool_use blocks."""

    JSON_ASK = (
        "\n\nAnswer with exactly one JSON object and nothing else: "
        '{"facts": [{"key": "<topic key>", "value": "<the fact>", '
        '"category": "preference|constraint|context"}]}. '
        "An empty list when nothing is worth keeping."
    )

    def __init__(self, client_factory: ClientFactory, model: Any = None, **_: Any) -> None:
        self._factory = client_factory
        self._model = getattr(model, "memory_model", None) or getattr(model, "model", None)
        self.messages = self  # so ``client.messages.create`` resolves here

    @staticmethod
    def transcript_of(messages: list[dict[str, Any]]) -> str:
        """The last customer message and the last assistant reply, as text; tool results
        never enter the transcript (RT-03)."""
        user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        parts = []
        if user:
            parts.append(f"Customer: {_plain_text(user.get('content'))}")
        if assistant:
            parts.append(f"Assistant: {_plain_text(assistant.get('content'))}")
        return "\n".join(parts)

    async def create(self, **request: Any) -> Any:
        prompt = _plain_text((request.get("messages") or [{}])[-1].get("content"))
        options = ClaudeAgentOptions(
            system_prompt=str(request.get("system") or "") + self.JSON_ASK,
            model=str(request.get("model") or self._model or "claude-sonnet-5"),
            allowed_tools=[],
            max_turns=1,
        )
        client = self._factory(options)
        if hasattr(client, "connect"):
            await client.connect()
        try:
            await client.query(prompt)
            text = ""
            async for message in client.receive_response():
                for block in getattr(message, "content", None) or []:
                    text += getattr(block, "text", "") or ""
        finally:
            if hasattr(client, "disconnect"):
                await client.disconnect()
        facts = _facts_from(text)
        blocks = [
            SimpleNamespace(type="tool_use", name="record_fact", input=fact, id=f"fact-{i}")
            for i, fact in enumerate(facts)
        ]
        usage = SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        return SimpleNamespace(
            id="sdk-shim", model=options.model, content=blocks, usage=usage, stop_reason="end_turn"
        )


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _facts_from(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return [f for f in facts or [] if isinstance(f, dict)]


async def streaming_shopping_runner(
    client: Any, text: str, toolset: Any, on_text: Callable[[str], None] | None = None
) -> TurnResult:
    """The reference ``run_turn`` with the text handed out as it arrives."""
    toolset.begin_turn()
    text = await shopping_sdk.ground_message(text, toolset)
    await client.query(text)
    result = TurnResult(text="")
    chunks: list[str] = []
    names_by_id: dict[str, str] = {}
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    if on_text is not None and block.text:
                        on_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    result.tool_calls.append(block.name)
                    result.tool_inputs.append(dict(block.input or {}))
                    names_by_id[block.id] = block.name
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    name = names_by_id.get(block.tool_use_id, "tool")
                    result.tool_errors.append(f"{name} — {_plain_text(block.content)}")
        elif isinstance(message, ResultMessage):
            result.cost_usd = message.total_cost_usd
            result.is_error = message.is_error
    result.text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    result.ui = toolset.drain_ui_events() if hasattr(toolset, "drain_ui_events") else []
    return result


class SdkShoppingAgent(_SdkAgentBase):
    executor_class = ShoppingToolExecutor

    def __init__(
        self,
        *,
        backend: StorefrontBackend,
        config: ShoppingAgentConfig,
        skills_dir: Path | None = None,
        memory_store: MemoryStore | None = None,
        memory_write_filter: MemoryWriteFilter | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.backend = backend
        self.config = config
        self.skills_root = shopping_sdk.SKILLS_DIR if skills_dir is None else skills_dir
        self.skills: SkillRegistry = shopping_sdk.load_skill_registry(self.skills_root)
        self.memory: MemoryRuntime = build_shopping_memory(
            config, memory_store or InMemoryMemoryStore(), memory_write_filter
        )

    def allowed_tools(self) -> list[str]:
        return lab_shopping_tool_names(self.config)

    def _build(
        self, session: ShoppingSessionContext
    ) -> tuple[ClaudeAgentOptions, CollectingShoppingToolset]:
        toolset = CollectingShoppingToolset(
            backend=self.backend,
            config=self.config,
            session=session,
            memory_store=self.memory.store,
            memory_write_filter=self.memory.write_filter,
        )
        ensure_project_skills(self.skills_root, shopping_sdk.RUNTIME_ROOT)
        options = ClaudeAgentOptions(
            system_prompt=shopping_sdk.build_system_prompt(self.config, self.skills),
            mcp_servers={shopping_sdk.SERVER_NAME: build_lab_shopping_server(toolset)},
            allowed_tools=self.allowed_tools(),
            tools=["Skill"],
            skills=self.skills.names,
            setting_sources=["project"],
            cwd=shopping_sdk.RUNTIME_ROOT,
            env={"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"},
            model=self.config.model,
            max_turns=self._max_turns,
            permission_mode="dontAsk",
            hooks=close_on_presentation_hook(toolset, self.config.close_on_presentation),
        )
        return options, toolset

    def _sync_in(self, toolset: CollectingShoppingToolset, state: ShoppingSessionState) -> None:
        toolset.state.seen_products.update(state.seen_products)

    def _sync_out(self, toolset: CollectingShoppingToolset, state: ShoppingSessionState) -> None:
        state.seen_products.update(toolset.state.seen_products)

    async def _after_turn(self, session: ShoppingSessionContext, toolset: Any) -> list[AgentEvent]:
        # The web app repaints the cart panel from this event; the executor emits it on
        # writes, but a read-only turn after a button add should still show the truth.
        try:
            cart = await self.backend.get_cart(session)
        except Exception:
            return []
        return [AgentEvent.cart_update(cart_payload(cart))]

    def _default_runner(self) -> TurnRunner:
        return streaming_shopping_runner


async def _shopping_runner(client: Any, text: str, toolset: Any) -> TurnResult:
    return await shopping_sdk.run_turn(client, text, toolset=toolset)


class SdkMerchantAgent(_SdkAgentBase):
    executor_class = MerchantToolExecutor

    def __init__(
        self,
        *,
        backend: MerchantBackend,
        config: MerchantAgentConfig,
        skills_dir: Path | None = None,
        memory_store: MemoryStore | None = None,
        memory_write_filter: MemoryWriteFilter | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.backend = backend
        self.config = config
        self.skills_root = merchant_sdk.SKILLS_DIR if skills_dir is None else skills_dir
        self.skills: SkillRegistry = merchant_sdk.load_skill_registry(self.skills_root)
        self.memory: MemoryRuntime = build_merchant_memory(
            config, memory_store or InMemoryMemoryStore(), memory_write_filter
        )

    def _build(
        self, session: MerchantSessionContext
    ) -> tuple[ClaudeAgentOptions, CollectingMerchantToolset]:
        toolset = CollectingMerchantToolset(
            backend=self.backend,
            config=self.config,
            session=session,
            memory_store=self.memory.store,
            memory_write_filter=self.memory.write_filter,
        )
        ensure_project_skills(self.skills_root, merchant_sdk.RUNTIME_ROOT)
        options = ClaudeAgentOptions(
            system_prompt=merchant_sdk.build_system_prompt(self.config, self.skills),
            mcp_servers={merchant_sdk.SERVER_NAME: build_merchant_server(toolset)},
            allowed_tools=merchant_tool_names(self.config),
            tools=["Skill"],
            skills=self.skills.names,
            setting_sources=["project"],
            cwd=merchant_sdk.RUNTIME_ROOT,
            env={"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"},
            model=self.config.model,
            max_turns=self._max_turns,
            permission_mode="dontAsk",
            hooks=close_on_presentation_hook(toolset, self.config.close_on_presentation),
        )
        return options, toolset

    def _sync_in(self, toolset: CollectingMerchantToolset, state: MerchantSessionState) -> None:
        mine = toolset.state
        mine.seen_listings.update(state.seen_listings)
        mine.read_listings |= state.read_listings
        mine.seen_changes.update(state.seen_changes)
        mine.seen_series.update(state.seen_series)
        if state.latest_snapshot is not None:
            mine.latest_snapshot = state.latest_snapshot
        mine.approved_change_ids = set(state.approved_change_ids)
        mine.host_action_change_ids = set(state.host_action_change_ids)

    def _sync_out(self, toolset: CollectingMerchantToolset, state: MerchantSessionState) -> None:
        mine = toolset.state
        state.seen_listings.update(mine.seen_listings)
        state.read_listings |= mine.read_listings
        state.seen_changes.update(mine.seen_changes)
        state.seen_series.update(mine.seen_series)
        if mine.latest_snapshot is not None:
            state.latest_snapshot = mine.latest_snapshot
        state.approved_change_ids = set(mine.approved_change_ids)
        state.host_action_change_ids = set(mine.host_action_change_ids)

    def _default_runner(self) -> TurnRunner:
        return streaming_merchant_runner


async def streaming_merchant_runner(
    client: Any, text: str, toolset: Any, on_text: Callable[[str], None] | None = None
) -> TurnResult:
    """The merchant reference ``run_turn`` with text handed out as it arrives."""
    toolset.begin_turn()
    if hasattr(merchant_sdk, "ground_message"):
        text = await merchant_sdk.ground_message(text, toolset)
    await client.query(text)
    result = TurnResult(text="")
    chunks: list[str] = []
    names_by_id: dict[str, str] = {}
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    if on_text is not None and block.text:
                        on_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    result.tool_calls.append(block.name)
                    result.tool_inputs.append(dict(block.input or {}))
                    names_by_id[block.id] = block.name
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    name = names_by_id.get(block.tool_use_id, "tool")
                    result.tool_errors.append(f"{name} — {_plain_text(block.content)}")
        elif isinstance(message, ResultMessage):
            result.cost_usd = message.total_cost_usd
            result.is_error = message.is_error
    result.text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    result.ui = toolset.drain_ui_events() if hasattr(toolset, "drain_ui_events") else []
    return result
