from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from backend.vm.guest import GuestClient, GuestError, GuestOperationError
from guest.agentd import MAX_REPLY_BYTES, encode_reply, handle_message, is_authorized, serve
from guest.files import file_list, file_read, file_write
from guest.shell import shell_exec
from guest.validation import OperationError


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_authorization_requires_exact_bearer_secret() -> None:
    assert is_authorized("Bearer s3cret", "s3cret")
    assert not is_authorized("Bearer wrong", "s3cret")
    assert not is_authorized("s3cret", "s3cret")
    assert not is_authorized(None, "s3cret")
    assert not is_authorized("Bearer ", "")


def test_shell_exec_returns_output_and_exit_code(tmp_path: Path) -> None:
    result = run(shell_exec({"command": "echo out; echo err >&2; exit 3", "cwd": str(tmp_path)}))
    assert result == {
        "exit_code": 3, "timed_out": False, "stdout": "out\n", "stderr": "err\n", "truncated": False,
    }


def test_shell_exec_kills_process_group_on_timeout(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    command = f"(sleep 3; touch {marker}) & sleep 30"
    result = run(shell_exec({"command": command, "timeout": 1, "cwd": str(tmp_path)}))
    assert result["timed_out"] and result["exit_code"] is None
    run(asyncio.sleep(3.5))
    assert not marker.exists()


def test_shell_exec_caps_output(tmp_path: Path) -> None:
    result = run(shell_exec({"command": "head -c 200000 /dev/zero | tr '\\0' x", "cwd": str(tmp_path)}))
    assert result["truncated"] and len(result["stdout"]) == 16_000 and result["exit_code"] == 0


@pytest.mark.parametrize(
    "args",
    [{}, {"command": ""}, {"command": 5}, {"command": "ls", "timeout": 0},
     {"command": "ls", "timeout": "5"}, {"command": "ls", "cwd": "/no/such/dir"}],
)
def test_shell_exec_rejects_invalid_arguments(args: dict[str, object]) -> None:
    with pytest.raises(OperationError):
        run(shell_exec(args))


def test_file_write_read_list_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "notes" / "a.txt"
    assert run(file_write({"path": str(target), "content": "héllo"}))["bytes_written"] == 6
    run(file_write({"path": str(target), "content": " again", "append": True}))
    read = run(file_read({"path": str(target)}))
    assert read["content"] == "héllo again" and not read["truncated"]
    assert run(file_read({"path": str(target), "max_bytes": 3}))["truncated"]

    listing = run(file_list({"path": str(tmp_path)}))
    assert listing["entries"] == [{"name": "notes", "type": "dir", "size": None}]
    with pytest.raises(OperationError, match="cannot read"):
        run(file_read({"path": str(tmp_path / "missing")}))
    with pytest.raises(OperationError):
        run(file_write({"path": str(target), "content": 123}))


def test_handle_message_never_raises() -> None:
    assert run(handle_message('{"id": 7, "op": "ping"}')) == {
        "id": 7, "ok": True, "result": {"pong": True},
    }
    for raw in ("not json", "[]", '{"id": 1, "op": "nope"}', '{"id": 1, "op": "ping", "args": []}',
                '{"id": 1, "op": "shell.exec", "args": {}}'):
        reply = run(handle_message(raw))
        assert reply["ok"] is False and reply["error"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_client_and_daemon_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        port = free_port()
        server = asyncio.create_task(serve("127.0.0.1", port, "s3cret"))
        try:
            client = GuestClient(port, "s3cret")
            await client.wait_ready(timeout=5, interval=0.05)
            result = await client.call("shell.exec", {"command": "echo hi", "cwd": str(tmp_path)})
            assert result["stdout"] == "hi\n"
            with pytest.raises(GuestOperationError, match="unknown operation"):
                await client.call("nope")

            intruder = GuestClient(port, "wrong")
            assert not await intruder.is_ready()
            with pytest.raises(GuestError, match="rejected"):
                await intruder.call("shell.exec", {"command": "id"})
        finally:
            server.cancel()
        assert not await GuestClient(port, "s3cret").is_ready()

    run(scenario())


def test_replies_are_json_serialisable(tmp_path: Path) -> None:
    reply = run(handle_message(json.dumps({"id": 1, "op": "file.list", "args": {"path": str(tmp_path)}})))
    assert json.loads(json.dumps(reply))["result"]["entries"] == []


# --- findings from the guest review ----------------------------------------------------------


def test_shell_exec_returns_when_the_command_leaves_something_running(tmp_path: Path) -> None:
    import time

    started = time.monotonic()
    result = run(shell_exec({"command": "sleep 20 & echo started", "timeout": 10, "cwd": str(tmp_path)}))
    assert time.monotonic() - started < 4  # not the 10 s timeout, and not the 20 s of the child
    assert result["stdout"] == "started\n" and result["exit_code"] == 0 and not result["timed_out"]


def test_shell_exec_times_out_even_when_a_child_escaped_the_kill(tmp_path: Path) -> None:
    import time

    started = time.monotonic()
    # setsid: the child leaves the process group, keeps the output pipe, and is not killed.
    command = "python3 -c \"import os,time; os.setsid(); time.sleep(20)\" & sleep 30"
    result = run(shell_exec({"command": command, "timeout": 1, "cwd": str(tmp_path)}))
    assert result["timed_out"] and time.monotonic() - started < 5


def test_a_long_file_is_read_in_parts(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("a" * 70_000 + "END")
    first = run(file_read({"path": str(path)}))
    assert first["truncated"] and first["next_offset"] == len(first["content"]) and first["size"] == 70_003
    rest = run(file_read({"path": str(path), "offset": first["next_offset"]}))
    assert rest["content"].endswith("END") and not rest["truncated"] and "next_offset" not in rest


def test_pipes_and_devices_are_refused_instead_of_blocking(tmp_path: Path) -> None:
    import os

    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    for operation, args in ((file_read, {"path": str(pipe)}), (file_write, {"path": str(pipe), "content": "x"})):
        with pytest.raises(OperationError, match="not a regular file"):
            run(asyncio.wait_for(operation(args), 3))


def test_replies_are_sent_as_plain_text_and_never_oversized() -> None:
    reply = encode_reply({"id": 7, "ok": True, "result": {"content": "\x00é" * 1000}})
    assert len(reply) < 8_000 and json.loads(reply)["result"]["content"] == "\x00é" * 1000
    huge = json.loads(encode_reply({"id": 7, "ok": True, "result": {"content": "x" * (MAX_REPLY_BYTES + 1)}}))
    assert huge == {"id": 7, "ok": False, "error": "the result is too large to return; ask for less"}
