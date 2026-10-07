from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

from backend.agents.loop import Outcome, RunLimits, StopReason, run_agent_loop
from backend.agents.runtime import RunError, RunService
from backend.agents.tools import ALL_TOOLS, GuestToolExecutor, store_screenshot, tools_for
from backend.config import ProviderSettings, Settings
from backend.db.database import create_db_engine, init_db
from backend.db.models import Agent, Approval, Message, Run, ToolCall
from backend.policy.approvals import ApprovalBroker
from backend.policy.engine import PolicyAction, PolicyDecision, PolicyEngine
from backend.providers.base import (
    ChatMessage,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolCallRequest,
    ToolSpec,
)
from backend.providers.openai_compatible import OpenAICompatibleProvider, parse_response
from backend.providers.registry import FallbackProvider, build_provider, resolve_api_key
from backend.vm.guest import GuestError
from backend.vm.lifecycle import QemuVMManager

FIXTURES = Path(__file__).parent / "fixtures"


def call(name: str = "shell_exec", **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=f"c{len(arguments)}", name=name, arguments=arguments)


class ScriptedProvider(ModelProvider):
    """Returns canned responses in order, repeating the last one."""

    def __init__(self, *responses: ModelResponse) -> None:
        self._responses = list(responses)
        self.requests: list[list[ChatMessage]] = []
        self.tools_seen: list[list[ToolSpec]] = []

    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> ModelResponse:
        self.requests.append(list(messages))
        self.tools_seen.append(list(tools or []))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class FakeExecutor:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[ToolCallRequest] = []
        self._delay = delay

    def operation_for(self, tool_name: str) -> str | None:
        return tool_name.replace("_", ".", 1)

    async def execute(self, call: ToolCallRequest) -> dict[str, Any]:
        self.calls.append(call)
        await asyncio.sleep(self._delay)
        return {"exit_code": 0, "stdout": "ok"}


class FakeObserver:
    """Records outcomes; answers approval requests with a fixed decision after a delay."""

    def __init__(self, approve: bool = True, approval_delay: float = 0.0) -> None:
        self.outcomes: list[str] = []
        self.operations: list[str] = []
        self.approvals: list[PolicyDecision] = []
        self._approve = approve
        self._approval_delay = approval_delay

    async def tool_started(self, call: ToolCallRequest, operation: str) -> int:
        self.operations.append(operation)
        return len(self.operations)

    async def request_approval(self, record_id: int, decision: PolicyDecision) -> bool:
        self.approvals.append(decision)
        await asyncio.sleep(self._approval_delay)
        return self._approve

    async def wait_until_agent_may_act(self) -> None:
        return None

    async def tool_finished(self, record_id: int, outcome: Outcome, result: dict[str, Any]) -> None:
        self.outcomes.append(outcome)


def loop(provider: ModelProvider, executor: FakeExecutor, policy: PolicyEngine | None = None,
         observer: FakeObserver | None = None, **limits: Any) -> tuple[Any, FakeObserver, list[ChatMessage]]:
    observer = observer or FakeObserver()
    messages = [ChatMessage(role="user", content="go")]
    result = asyncio.run(
        run_agent_loop(provider, messages, [], executor, (policy or PolicyEngine()).evaluate,
                       RunLimits(**limits), observer)
    )
    return result, observer, messages


def test_loop_runs_tools_then_returns_answer() -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="ls")]), ModelResponse(text="done")
    )
    executor = FakeExecutor()
    result, observer, messages = loop(provider, executor)
    assert result.text == "done" and result.stop_reason == StopReason.COMPLETED
    assert observer.outcomes == ["allow"] and observer.operations == ["shell.exec"]
    assert [m.role for m in messages] == ["user", "assistant", "tool"]
    assert json.loads(messages[-1].content)["stdout"] == "ok"
    assert messages[-1].tool_call_id == messages[-2].tool_calls[0].id


def test_loop_stops_on_repeated_identical_calls() -> None:
    provider = ScriptedProvider(ModelResponse(tool_calls=[call(command="ls")]))
    executor = FakeExecutor()
    result, _, _ = loop(provider, executor, max_repeated_identical_calls=2)
    assert result.stop_reason == StopReason.REPEAT_LIMIT and len(executor.calls) == 2


def test_loop_stops_at_tool_call_limit() -> None:
    responses = [ModelResponse(tool_calls=[call(command=f"echo {i}")]) for i in range(10)]
    executor = FakeExecutor()
    result, _, _ = loop(ScriptedProvider(*responses), executor, max_tool_calls_per_run=3)
    assert result.stop_reason == StopReason.TOOL_LIMIT and len(executor.calls) == 3


def test_loop_stops_at_runtime_limit() -> None:
    responses = [ModelResponse(tool_calls=[call(command=f"echo {i}")]) for i in range(50)]
    result, _, _ = loop(ScriptedProvider(*responses), FakeExecutor(delay=0.05),
                        max_runtime_seconds=0.12)
    assert result.stop_reason == StopReason.TIMEOUT and "time limit" in result.text


def test_loop_reports_blocked_and_invalid_calls_to_the_model() -> None:
    class BlockCurl(PolicyEngine):
        async def evaluate(self, operation: str, arguments: dict[str, Any], task: str = "",
                           use_decision_provider: bool = False) -> PolicyDecision:
            if "curl" in arguments.get("command", ""):
                return PolicyDecision(action=PolicyAction.REJECT, reason="no network")
            return PolicyDecision(action=PolicyAction.ALLOW)

    bad = ToolCallRequest(id="b", name="shell_exec", arguments_error="arguments are not valid JSON")
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="curl x"), bad]), ModelResponse(text="ok")
    )
    executor = FakeExecutor()
    _, observer, messages = loop(provider, executor, BlockCurl())
    assert executor.calls == []
    assert observer.outcomes == ["blocked", "invalid"]
    assert "no network" in messages[2].content and "not valid JSON" in messages[3].content


def test_loop_asks_for_approval_before_destructive_commands() -> None:
    def scripted() -> ScriptedProvider:
        return ScriptedProvider(
            ModelResponse(tool_calls=[call(command="rm -rf ~/project")]), ModelResponse(text="ok")
        )

    executor = FakeExecutor()
    _, observer, _ = loop(scripted(), executor, observer=FakeObserver(approve=True))
    assert observer.outcomes == ["approved"] and len(executor.calls) == 1
    assert observer.approvals[0].risk == "high" and "recursively" in observer.approvals[0].reason

    executor = FakeExecutor()
    _, observer, messages = loop(scripted(), executor, observer=FakeObserver(approve=False))
    assert observer.outcomes == ["rejected"] and executor.calls == []
    assert "rejected" in messages[-1].content


def test_waiting_for_the_user_does_not_use_up_the_run_time() -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="rm -rf ~/project")]), ModelResponse(text="finished")
    )
    observer = FakeObserver(approve=True, approval_delay=0.4)
    result, _, _ = loop(provider, FakeExecutor(), observer=observer, max_runtime_seconds=0.2)
    assert result.stop_reason == StopReason.COMPLETED and result.text == "finished"


# --- provider adapter -------------------------------------------------------------------


def test_parse_recorded_ollama_tool_call() -> None:
    response = parse_response(json.loads((FIXTURES / "ollama_tool_call.json").read_text()))
    (tool_call,) = response.tool_calls
    assert tool_call.name == "shell_exec" and tool_call.id
    assert isinstance(tool_call.arguments.get("command"), str)
    assert "<think>" not in response.text


def test_parse_response_variants() -> None:
    def message(**fields: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", **fields}}]}

    assert parse_response(message(content="<think>hmm\nplan</think>\n\nHello")).text == "Hello"
    as_object = parse_response(message(content=None, tool_calls=[
        {"function": {"name": "file_read", "arguments": {"path": "a"}}}]))
    assert as_object.tool_calls[0].arguments == {"path": "a"} and as_object.tool_calls[0].id
    broken = parse_response(message(tool_calls=[
        {"id": "x", "function": {"name": "file_read", "arguments": '{"path": '}}]))
    assert broken.tool_calls[0].arguments_error
    with pytest.raises(ProviderError):
        parse_response({"choices": []})


def test_provider_sends_neutral_messages_in_openai_format() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    provider = OpenAICompatibleProvider(
        "http://local/v1/", "some-model", transport=httpx.MockTransport(handler)
    )
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", tool_calls=[call(command="ls")]),
        ChatMessage(role="tool", content="{}", tool_call_id="c1"),
    ]
    response = asyncio.run(provider.chat(messages, [tool.spec for tool in ALL_TOOLS]))
    assert response.text == "hi"
    assert seen["url"] == "http://local/v1/chat/completions" and seen["auth"] is None
    body = seen["body"]
    assert body["model"] == "some-model" and len(body["tools"]) == len(ALL_TOOLS)
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'
    assert body["messages"][2]["tool_call_id"] == "c1"


def test_provider_errors_become_provider_error() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    provider = OpenAICompatibleProvider("http://x/v1", "m", transport=httpx.MockTransport(failing))
    with pytest.raises(ProviderError, match="404.*model not found"):
        asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = OpenAICompatibleProvider("http://x/v1", "m", transport=httpx.MockTransport(refused))
    with pytest.raises(ProviderError, match="Is it running"):
        asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))


def test_api_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    local = ProviderSettings(type="openai-compatible", base_url="http://x/v1", api_key="none")
    assert resolve_api_key("ollama", local) is None
    hosted = ProviderSettings(type="openai-compatible", base_url="http://x/v1", api_key_env="K_TEST")
    monkeypatch.delenv("K_TEST", raising=False)
    with pytest.raises(ProviderError, match="K_TEST"):
        resolve_api_key("grok", hosted)
    monkeypatch.setenv("K_TEST", "sk-123")
    assert resolve_api_key("grok", hosted) == "sk-123"
    with pytest.raises(ProviderError, match="not supported"):
        build_provider("a", ProviderSettings(type="made-up", api_key="x"), "m")


def test_fallback_provider_used_when_primary_fails() -> None:
    class Failing(ModelProvider):
        async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
            raise ProviderError("down")

    provider = FallbackProvider(Failing(), ScriptedProvider(ModelResponse(text="from fallback")))
    assert asyncio.run(provider.chat([])).text == "from fallback"


# --- tools and run service ----------------------------------------------------------------


def test_tools_follow_agent_permissions() -> None:
    agent = Agent(name="a", provider="p", model="m", vm_disk_path="/d", perm_shell=False)
    names = [tool.spec.name for tool in tools_for(agent)]
    assert "shell_exec" not in names and "file_read" in names and "browser_goto" in names
    agent.perm_browser = False
    assert [tool.spec.name for tool in tools_for(agent)] == [
        "file_read", "file_write", "file_list",
        "memory_remember", "memory_recall", "memory_forget", "db_sql",
        "channel_read", "channel_post"]


def test_executor_maps_tools_to_guest_operations() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.seen: list[tuple[str, dict[str, Any], float]] = []

        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            self.seen.append((op, args or {}, timeout))
            if op == "file.read":
                raise GuestError("guest daemon unavailable")
            return {"exit_code": 0}

    client = FakeClient()
    executor = GuestToolExecutor(client, ALL_TOOLS, Path("/nonexistent"))  # type: ignore[arg-type]
    assert asyncio.run(executor.execute(call(command="ls", timeout=120))) == {"exit_code": 0}
    assert client.seen[0] == ("shell.exec", {"command": "ls", "timeout": 120}, 120.0)
    assert "unavailable" in asyncio.run(executor.execute(call("file_read", path="a")))["error"]
    assert "unknown" in asyncio.run(executor.execute(call("made_up_tool", url="x")))["error"]


def test_screenshots_are_stored_and_never_returned_inline(tmp_path: Path) -> None:
    import base64

    image = b"\xff\xd8fake-jpeg"
    result = store_screenshot(
        {"url": "https://example.com", "image_format": "jpeg",
         "image_base64": base64.b64encode(image).decode()}, tmp_path / "shots")
    assert "image_base64" not in result and result["screenshot"].endswith(".jpg")
    assert (tmp_path / "shots" / result["screenshot"]).read_bytes() == image
    assert "error" in store_screenshot({"image_base64": "not base64!"}, tmp_path)
    assert store_screenshot({"exit_code": 0}, tmp_path) == {"exit_code": 0}


class FakeGuest:
    def __init__(self, port: int, secret: str) -> None:
        self.calls: list[str] = []

    async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self.calls.append(op)
        return {"exit_code": 0, "stdout": "hello\n"}


def make_service(tmp_path: Path, provider: ModelProvider) -> tuple[RunService, Any, int]:
    """A run service on a temp database, with the guest and the model faked."""
    settings = Settings(home=tmp_path)
    settings.ensure_layout()
    engine = create_db_engine(settings.db_path)
    init_db(engine)
    with Session(engine) as session:
        agent = Agent(name="a1", provider="ollama", model="m", system_prompt="Be brief.",
                      vm_disk_path=str(tmp_path / "disk.qcow2"), vm_daemon_port=18000)
        session.add(agent)
        session.commit()
        agent_id = agent.id
    assert agent_id is not None
    service = RunService(engine, settings, QemuVMManager(settings), PolicyEngine(),
                         ApprovalBroker(), provider_factory=lambda name, cfg, model: provider,
                         client_factory=FakeGuest)  # type: ignore[arg-type]
    return service, engine, agent_id


def test_run_service_persists_messages_and_tool_calls(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="echo hello")]), ModelResponse(text="It said hello.")
    )
    service, engine, agent_id = make_service(tmp_path, provider)

    async def scenario() -> int:
        run_id = service.start(agent_id, "say hello")
        with pytest.raises(RunError):
            service.start(agent_id, "again")
        await service.wait(agent_id)
        return run_id

    run_id = asyncio.run(scenario())
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status == "completed" and run.finished_at
        messages = session.exec(select(Message).order_by(Message.id)).all()  # type: ignore[arg-type]
        assert [(m.role, m.content) for m in messages] == [
            ("user", "say hello"), ("assistant", "It said hello.")]
        (tool_call,) = session.exec(select(ToolCall)).all()
        assert tool_call.tool == "shell.exec" and tool_call.decision == "allow"
        assert tool_call.finished_at is not None
        assert json.loads(tool_call.arguments_json) == {"command": "echo hello"}
    system, user = provider.requests[0][:2]
    assert system.role == "system" and system.content.startswith("Be brief.")
    assert user.content == "say hello"


def test_run_service_records_provider_failure(tmp_path: Path) -> None:
    class Down(ModelProvider):
        async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
            raise ProviderError("request to http://localhost:11434/v1 failed")

    service, engine, agent_id = make_service(tmp_path, Down())

    async def scenario() -> int:
        run_id = service.start(agent_id, "hi")
        await service.wait(agent_id)
        return run_id

    run_id = asyncio.run(scenario())
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status == "failed" and "11434" in (run.error or "")
        run.status = "running"
        session.add(run)
        session.commit()
    service.mark_interrupted()
    with Session(engine) as session:
        assert session.get(Run, run_id).status == "interrupted"  # type: ignore[union-attr]


def test_manual_control_holds_tool_calls(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="echo hello")]), ModelResponse(text="done")
    )
    service, engine, agent_id = make_service(tmp_path, provider)

    async def scenario() -> None:
        service.set_manual(agent_id, True)
        assert service.is_manual(agent_id)
        service.start(agent_id, "go")
        await asyncio.sleep(0.2)
        with Session(engine) as session:
            assert session.exec(select(ToolCall)).one().finished_at is None  # proposed, not run
        service.set_manual(agent_id, False)
        await service.wait(agent_id)
        with Session(engine) as session:
            assert session.exec(select(ToolCall)).one().decision == "allow"

    asyncio.run(scenario())


def test_run_pauses_for_approval_and_resumes_on_answer(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="sudo apt remove postgresql")]),
        ModelResponse(text="Removed."),
    )
    service, engine, agent_id = make_service(tmp_path, provider)
    broker: ApprovalBroker = service._approvals  # noqa: SLF001

    async def pending_approval() -> Approval:
        for _ in range(100):
            with Session(engine) as session:
                approval = session.exec(select(Approval).where(Approval.status == "pending")).first()
            if approval is not None:
                return approval
            await asyncio.sleep(0.02)
        raise AssertionError("no approval was requested")

    async def scenario() -> None:
        service.start(agent_id, "remove postgres")
        approval = await pending_approval()
        assert approval.risk == "high" and "removes installed software" in (approval.reason or "")
        assert service.is_busy(agent_id)
        assert approval.id is not None and broker.resolve(approval.id, True)
        assert not broker.resolve(approval.id, True)  # already answered
        await service.wait(agent_id)

    asyncio.run(scenario())
    with Session(engine) as session:
        (tool_call,) = session.exec(select(ToolCall)).all()
        assert tool_call.decision == "approved"
        assert session.exec(select(Approval)).one().status == "approved"
        assert session.exec(select(Run)).one().status == "completed"


def test_cancel_stops_a_run_waiting_for_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider(ModelResponse(tool_calls=[call(command="rm -rf ~/x")]))
    service, engine, agent_id = make_service(tmp_path, provider)

    async def scenario() -> None:
        service.start(agent_id, "clean up")
        await asyncio.sleep(0.2)
        assert await service.cancel(agent_id)
        assert not await service.cancel(agent_id)

    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.exec(select(Run)).one().status == "cancelled"
        assert session.exec(select(ToolCall)).one().decision == "cancelled"
        assert session.exec(select(Approval)).one().status == "expired"


# --- Anthropic adapter ----------------------------------------------------------------------


def test_anthropic_request_shape() -> None:
    from backend.providers.anthropic import RAW_CONTENT, to_request, to_tool

    raw = [{"type": "thinking", "thinking": "", "signature": "sig"},
           {"type": "tool_use", "id": "t1", "name": "shell_exec", "input": {"command": "ls"}}]
    system, wire = to_request([
        ChatMessage(role="system", content="Be brief."),
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", tool_calls=[call(command="ls")], provider_data={RAW_CONTENT: raw}),
        ChatMessage(role="tool", content='{"exit_code": 0}', tool_call_id="t1"),
        ChatMessage(role="tool", content='{"error": "boom"}', tool_call_id="t2"),
        ChatMessage(role="assistant", content="earlier answer"),
    ])
    assert system == "Be brief."
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant"]
    assert wire[1]["content"] is raw  # thinking blocks replayed unchanged
    results = wire[2]["content"]  # both results in one user message
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "is_error" not in results[0] and results[1]["is_error"] is True
    assert wire[3]["content"] == [{"type": "text", "text": "earlier answer"}]
    spec = ALL_TOOLS[0].spec
    assert to_tool(spec) == {"name": spec.name, "description": spec.description,
                             "input_schema": spec.parameters}


def test_anthropic_provider_parses_responses_and_errors() -> None:
    import anthropic
    from anthropic.types import Message

    from backend.providers.anthropic import RAW_CONTENT, AnthropicProvider

    def message(stop_reason: str, content: list[dict[str, Any]]) -> Message:
        return Message.model_validate({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "some-model",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    class FakeMessages:
        def __init__(self, reply: Any) -> None:
            self.reply, self.params = reply, {}

        async def create(self, **params: Any) -> Any:
            self.params = params
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    class FakeClient:
        def __init__(self, reply: Any) -> None:
            self.messages = FakeMessages(reply)

    tool_turn = message("tool_use", [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "t1", "name": "shell_exec", "input": {"command": "ls"}},
    ])
    client = FakeClient(tool_turn)
    provider = AnthropicProvider("some-model", client=client)
    response = asyncio.run(provider.chat(
        [ChatMessage(role="system", content="S"), ChatMessage(role="user", content="go")],
        [tool.spec for tool in ALL_TOOLS]))
    assert response.text == "Checking."
    assert response.tool_calls[0].arguments == {"command": "ls"} and response.tool_calls[0].id == "t1"
    assert [b["type"] for b in response.provider_data[RAW_CONTENT]] == ["thinking", "text", "tool_use"]
    sent = client.messages.params
    assert sent["model"] == "some-model" and sent["system"] == "S" and sent["max_tokens"] > 0
    assert "tool_choice" not in sent and "thinking" not in sent and len(sent["tools"]) == len(ALL_TOOLS)

    final = AnthropicProvider("m", client=FakeClient(message("end_turn", [{"type": "text", "text": "Done."}])))
    answer = asyncio.run(final.chat([ChatMessage(role="user", content="hi")]))
    assert answer.text == "Done." and answer.provider_data is None

    refused = AnthropicProvider("m", client=FakeClient(message("refusal", [])))
    with pytest.raises(ProviderError, match="declined"):
        asyncio.run(refused.chat([ChatMessage(role="user", content="hi")]))

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    down = AnthropicProvider("m", client=FakeClient(anthropic.APIConnectionError(request=request)))
    with pytest.raises(ProviderError, match="reach"):
        asyncio.run(down.chat([ChatMessage(role="user", content="hi")]))


def test_loop_passes_provider_data_back_untouched() -> None:
    marker = {"anthropic_content": [{"type": "thinking", "signature": "sig"}]}
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="ls")], provider_data=marker), ModelResponse(text="ok")
    )
    _, _, messages = loop(provider, FakeExecutor())
    assert messages[1].provider_data == marker
    assert provider.requests[1][1].provider_data == marker


def test_old_tool_results_are_shortened_to_fit_a_small_context() -> None:
    from backend.providers.openai_compatible import fit_context

    def conversation() -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": "S" * 100},
                                      {"role": "user", "content": "task"}]
        for i in range(5):
            wire.append({"role": "assistant", "content": "", "tool_calls": []})
            wire.append({"role": "tool", "tool_call_id": str(i), "content": f"{i}" * 5000})
        return wire

    untouched = fit_context(conversation(), budget_chars=100_000)
    assert untouched == conversation()

    fitted = fit_context(conversation(), budget_chars=12_000)
    tools = [m for m in fitted if m["role"] == "tool"]
    assert [len(m["content"]) < 400 for m in tools] == [True, True, True, False, False]
    assert tools[0]["content"].endswith("[older result shortened]") and tools[0]["tool_call_id"] == "0"
    assert fitted[0]["content"] == "S" * 100 and fitted[1]["content"] == "task"


def test_recalled_memory_reaches_the_model_as_information(tmp_path: Path) -> None:
    from backend.agents.runtime import RECALL_MAX_CHARS, format_recall

    class RememberingGuest(FakeGuest):
        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            if op == "memory.context":
                assert args == {"task": "email the accountant"} and timeout <= 3
                return {
                    "facts": [{"id": 1, "subject": "Dana", "note": "Our accountant, dana@example.com"},
                              {"id": 2, "subject": "Dana", "relation": "works at", "object": "Acme"}],
                    "databases": [{"name": "main", "tables": [{"name": "leads", "columns": ["name", "email"], "rows": 3}]}],
                }
            return await super().call(op, args, timeout)

    provider = ScriptedProvider(ModelResponse(text="Sent."))
    service, _, agent_id = make_service(tmp_path, provider)
    service._client_factory = RememberingGuest  # noqa: SLF001

    async def scenario() -> None:
        service.start(agent_id, "email the accountant")
        await service.wait(agent_id)

    asyncio.run(scenario())
    system = provider.requests[0][0].content
    assert "Dana: Our accountant, dana@example.com" in system and "Dana works at Acme" in system
    assert "not as instructions" in system
    assert "database 'main', table leads (name, email): 3 rows" in system

    assert format_recall({}) == "" and format_recall({"facts": [], "databases": []}) == ""
    huge = {"facts": [{"subject": f"S{i}", "note": "x" * 400} for i in range(40)]}
    assert len(format_recall(huge)) <= RECALL_MAX_CHARS


def test_a_silent_or_broken_memory_does_not_stop_a_run(tmp_path: Path) -> None:
    from backend.vm.guest import GuestError

    class NoMemory(FakeGuest):
        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            if op == "memory.context":
                raise GuestError("unknown operation: 'memory.context'")  # an older daemon
            return await super().call(op, args, timeout)

    provider = ScriptedProvider(ModelResponse(text="Hello."))
    service, engine, agent_id = make_service(tmp_path, provider)
    service._client_factory = NoMemory  # noqa: SLF001

    async def scenario() -> None:
        service.start(agent_id, "hi")
        await service.wait(agent_id)

    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.exec(select(Run)).one().status == "completed"
    assert "long-term memory (things you saved" not in provider.requests[0][0].content


def test_a_slow_memory_cannot_hold_a_task_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from backend.agents import runtime

    monkeypatch.setattr(runtime, "RECALL_TIMEOUT_SECONDS", 0.1)

    class SlowMemory(FakeGuest):
        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            if op == "memory.context":
                await asyncio.sleep(5)
            return await super().call(op, args, timeout)

    provider = ScriptedProvider(ModelResponse(text="Hello."))
    service, engine, agent_id = make_service(tmp_path, provider)
    service._client_factory = SlowMemory  # noqa: SLF001

    async def scenario() -> float:
        started = time.monotonic()
        service.start(agent_id, "hi")
        await service.wait(agent_id)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 2
    with Session(engine) as session:
        assert session.exec(select(Run)).one().status == "completed"


def test_notifier_hears_about_approvals_and_finished_runs(tmp_path: Path) -> None:
    from backend.notify import Notifier

    class Recorder(Notifier):
        def __init__(self) -> None:
            self.events: list[Any] = []

        def attention_changed(self, waiting: int) -> None:
            self.events.append(("waiting", waiting))

        def run_ended(self, agent_name: str, succeeded: bool) -> None:
            self.events.append(("ended", agent_name, succeeded))

    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="rm -rf ~/old")]), ModelResponse(text="Removed.")
    )
    service, engine, agent_id = make_service(tmp_path, provider)
    service.notifier = recorder = Recorder()
    broker: ApprovalBroker = service._approvals  # noqa: SLF001

    async def scenario() -> None:
        service.start(agent_id, "clean up")
        for _ in range(100):
            if recorder.events:
                break
            await asyncio.sleep(0.02)
        assert recorder.events == [("waiting", 1)]
        with Session(engine) as session:
            approval = session.exec(select(Approval)).one()
        assert approval.id is not None and broker.resolve(approval.id, True)
        await service.wait(agent_id)

    asyncio.run(scenario())
    assert recorder.events == [("waiting", 1), ("waiting", 0), ("ended", "a1", True)]


def test_notifier_is_told_of_failures_but_not_of_user_cancellations(tmp_path: Path) -> None:
    from backend.notify import Notifier

    class Recorder(Notifier):
        def __init__(self) -> None:
            self.events: list[Any] = []

        def attention_changed(self, waiting: int) -> None:
            self.events.append(("waiting", waiting))

        def run_ended(self, agent_name: str, succeeded: bool) -> None:
            self.events.append(("ended", agent_name, succeeded))

    class Down(ModelProvider):
        async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
            raise ProviderError("unreachable")

    service, _, agent_id = make_service(tmp_path / "a", Down())
    service.notifier = failed = Recorder()

    async def fail() -> None:
        service.start(agent_id, "hi")
        await service.wait(agent_id)

    (tmp_path / "a").mkdir(exist_ok=True)
    asyncio.run(fail())
    assert failed.events == [("ended", "a1", False)]

    waiting_provider = ScriptedProvider(ModelResponse(tool_calls=[call(command="rm -rf ~/x")]))
    service, _, agent_id = make_service(tmp_path / "b", waiting_provider)
    service.notifier = cancelled = Recorder()

    async def cancel() -> None:
        service.start(agent_id, "clean")
        await asyncio.sleep(0.2)
        await service.cancel(agent_id)

    asyncio.run(cancel())
    assert cancelled.events == [("waiting", 1), ("waiting", 0)]  # badge cleared, no "finished" ping
