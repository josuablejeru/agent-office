"""Split DNS: giving agent VMs this Mac's view of name resolution."""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from backend.net import dns
from backend.net.dns import (
    DnsForwarder,
    DnsView,
    parse_scutil,
    parse_server,
    question_name,
    servfail,
)
from backend.vm.seed import GUEST_SOURCE_DIR, stage_seed
from guest import network

FIXTURES = Path(__file__).parent / "fixtures"

VPN_SCUTIL = """DNS configuration

resolver #1
  search domain[0] : corp.example
  search domain[1] : eng.corp.example
  nameserver[0] : 192.168.1.1
  nameserver[1] : 192.168.1.2
  if_index : 14 (en0)
  flags    : Request A records
  reach    : 0x00020002 (Reachable,Directly Reachable Address)

resolver #2
  domain   : corp.example
  nameserver[0] : 10.20.0.53
  nameserver[1] : 10.20.1.53
  flags    : Supplemental, Request A records
  reach    : 0x00000002 (Reachable)
  order    : 101000

resolver #3
  domain   : eng.corp.example
  nameserver[0] : 10.30.0.53
  port     : 5353
  order    : 101200

resolver #4
  domain   : local
  options  : mdns
  timeout  : 5

DNS configuration (for scoped queries)

resolver #1
  domain   : scoped-only.example
  nameserver[0] : 172.16.0.1
  if_index : 20 (utun4)
"""


def query(name: str, query_id: int = 0x1234) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", 1, 1)


def answer(request: bytes, address: str) -> bytes:
    header = request[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
    record = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
    return header + request[12:] + record


def answered_address(reply: bytes) -> str:
    return socket.inet_ntoa(reply[-4:])


def rcode(reply: bytes) -> int:
    return struct.unpack("!H", reply[2:4])[0] & 0x000F


def test_vpn_style_configuration_is_understood() -> None:
    view = parse_scutil(VPN_SCUTIL)
    assert view.default_servers == [("192.168.1.1", 53), ("192.168.1.2", 53)]
    assert view.search_domains == ["corp.example", "eng.corp.example"]
    assert {rule.domain: rule.servers for rule in view.rules} == {
        "corp.example": [("10.20.0.53", 53), ("10.20.1.53", 53)],
        "eng.corp.example": [("10.30.0.53", 5353)],
    }  # no .local (Bonjour), nothing from the per-interface section


def test_names_go_to_the_most_specific_rule() -> None:
    view = parse_scutil(VPN_SCUTIL)
    assert view.servers_for("wiki.corp.example") == [("10.20.0.53", 53), ("10.20.1.53", 53)]
    assert view.servers_for("CI.Eng.Corp.Example.") == [("10.30.0.53", 5353)]
    assert view.servers_for("corp.example") == [("10.20.0.53", 53), ("10.20.1.53", 53)]
    assert view.servers_for("example.com") == [("192.168.1.1", 53), ("192.168.1.2", 53)]
    assert view.servers_for("notcorp.example") == [("192.168.1.1", 53), ("192.168.1.2", 53)]


def test_this_macs_real_configuration_parses() -> None:
    view = parse_scutil((FIXTURES / "scutil_dns.txt").read_text())
    assert view.default_servers and all(port == 53 for _, port in view.default_servers)
    assert all(rule.domain != "local" and rule.servers for rule in view.rules)
    assert parse_scutil("") == DnsView() and parse_scutil("garbage\nresolver #x") == DnsView()


def test_server_addresses() -> None:
    assert parse_server("10.1.2.3") == ("10.1.2.3", 53)
    assert parse_server("10.1.2.3:5353") == ("10.1.2.3", 5353)
    assert parse_server("fd00::1") == ("fd00::1", 53)
    assert parse_server("[fd00::1]:5353") == ("fd00::1", 5353)
    assert parse_server("") is None


def test_question_name_and_malformed_packets() -> None:
    assert question_name(query("Wiki.Corp.Example")) == "wiki.corp.example"
    for bad in (b"", b"\x00" * 11, query("a.b")[:14], struct.pack("!HHHHHH", 1, 0, 0, 0, 0, 0)):
        assert question_name(bad) is None
    failed = servfail(query("a.example", 0xBEEF))
    assert failed[:2] == b"\xbe\xef" and rcode(failed) == 2 and failed[2] & 0x80


class FakeServer(asyncio.DatagramProtocol):
    """A DNS server that answers every question with one address, or stays silent."""

    def __init__(self, address: str | None) -> None:
        self.address, self.seen = address, []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.seen.append(question_name(data))
        if self.address:
            self.transport.sendto(answer(data, self.address), addr)  # type: ignore[attr-defined]


async def fake_server(address: str | None) -> tuple[FakeServer, int]:
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: FakeServer(address), local_addr=("127.0.0.1", 0))
    return protocol, transport.get_extra_info("sockname")[1]


async def ask(port: int, name: str) -> bytes:
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: dns._Exchange(query(name)), remote_addr=("127.0.0.1", port))  # noqa: SLF001
    try:
        return await asyncio.wait_for(protocol.reply, 5)
    finally:
        transport.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_forwarder_routes_internal_and_public_names_to_different_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        public, public_port = await fake_server("93.184.216.34")
        internal, internal_port = await fake_server("10.9.8.7")
        dead, dead_port = await fake_server(None)

        async def fake_scutil(*args: str, timeout: float = 5.0) -> str:
            return f"DNS configuration\n\nresolver #1\n  nameserver[0] : 127.0.0.1\n  port     : {public_port}\n"

        monkeypatch.setattr(dns, "run_command", fake_scutil)
        forwarder = DnsForwarder(
            free_port(),
            {"corp.example": [f"127.0.0.1:{internal_port}"],
             "flaky.example": [f"127.0.0.1:{dead_port}", f"127.0.0.1:{internal_port}"],
             "down.example": [f"127.0.0.1:{dead_port}"]},
            timeout=0.3,
        )
        await forwarder.start()
        try:
            assert forwarder.running
            assert answered_address(await ask(forwarder.port, "wiki.corp.example")) == "10.9.8.7"
            assert answered_address(await ask(forwarder.port, "example.com")) == "93.184.216.34"
            assert internal.seen == ["wiki.corp.example"] and public.seen == ["example.com"]
            # first server silent: the second one answers
            assert answered_address(await ask(forwarder.port, "x.flaky.example")) == "10.9.8.7"
            # nobody answers: a prompt failure, not a hang
            assert rcode(await ask(forwarder.port, "x.down.example")) == 2
            assert await forwarder.guest_settings() == {"port": forwarder.port, "search_domains": []}
        finally:
            forwarder.stop()
        assert await forwarder.guest_settings() is None

    asyncio.run(scenario())


def test_forwarder_that_cannot_listen_is_simply_off() -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
            taken.bind(("127.0.0.1", 0))
            forwarder = DnsForwarder(taken.getsockname()[1])
            await forwarder.start()
            assert not forwarder.running and await forwarder.guest_settings() is None

    asyncio.run(scenario())


def test_guest_resolver_settings() -> None:
    text = network.render_drop_in(47653, ["corp.example", "bad domain; rm -rf /", "eng.corp.example"])
    assert "DNS=10.0.2.2:47653\n" in text  # the forwarder alone: see render_drop_in
    assert "Domains=~. corp.example eng.corp.example\n" in text and "rm -rf" not in text


def test_seed_carries_dns_settings_only_when_the_forwarder_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_seed(tmp_path / "with", "a1", "s", GUEST_SOURCE_DIR, {"port": 47653, "search_domains": ["corp.example"]})
    written = tmp_path / "with" / "guest" / "host_dns.json"
    assert json.loads(written.read_text()) == {"port": 47653, "search_domains": ["corp.example"]}
    stage_seed(tmp_path / "without", "a1", "s", GUEST_SOURCE_DIR)
    assert not (tmp_path / "without" / "guest" / "host_dns.json").exists()

    monkeypatch.setattr(network, "SETTINGS_FILE", written)
    assert "DNS=10.0.2.2:47653" in (network.wanted_drop_in() or "")
    monkeypatch.setattr(network, "SETTINGS_FILE", tmp_path / "missing.json")
    assert network.wanted_drop_in() is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"port": 53}')
    monkeypatch.setattr(network, "SETTINGS_FILE", bad)
    assert network.wanted_drop_in() is None  # a privileged or nonsensical port is ignored


def test_large_answers_work_over_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            (length,) = struct.unpack("!H", await reader.readexactly(2))
            reply = answer(await reader.readexactly(length), "10.1.1.1")
            writer.write(struct.pack("!H", len(reply)) + reply)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = server.sockets[0].getsockname()[1]

        async def no_system_rules(*args: str, timeout: float = 5.0) -> str:
            return ""

        monkeypatch.setattr(dns, "run_command", no_system_rules)
        forwarder = DnsForwarder(free_port(), {"corp.example": [f"127.0.0.1:{upstream_port}"]}, timeout=1)
        await forwarder.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", forwarder.port)
            request = query("big.corp.example")
            writer.write(struct.pack("!H", len(request)) + request)
            await writer.drain()
            (length,) = struct.unpack("!H", await reader.readexactly(2))
            assert answered_address(await reader.readexactly(length)) == "10.1.1.1"
            writer.close()
        finally:
            forwarder.stop()
            server.close()

    asyncio.run(scenario())


def test_a_burst_of_lookups_reads_the_system_configuration_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def slow_scutil(*args: str, timeout: float = 5.0) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ""

    async def scenario() -> None:
        monkeypatch.setattr(dns, "run_command", slow_scutil)
        forwarder = DnsForwarder(free_port(), timeout=0.1)
        await asyncio.gather(*(forwarder.resolve(query(f"host{i}.example")) for i in range(50)))

    asyncio.run(scenario())
    assert calls == 1
