"""A DNS forwarder that gives agent VMs the same view of DNS as this Mac.

macOS can send different domains to different DNS servers ("split DNS"): a VPN
typically registers the company's internal domains with its own name servers.
A VM only learns the Mac's single default server, so internal names would not
resolve inside an agent's computer.

This forwarder listens on host loopback. Each query from a VM is passed, as
is, to the server macOS would use for that name, and the answer is passed
back. Nothing is cached and no answers are made up here.
"""

from __future__ import annotations

import asyncio
import re
import struct
import time
from typing import Any

from pydantic import BaseModel, Field

from backend.logging_config import get_logger
from backend.vm.capabilities import run_command
from backend.vm.ports import LOOPBACK

log = get_logger("dns")

DNS_PORT = 53
UPSTREAM_TIMEOUT_SECONDS = 3.0
CONFIG_TTL_SECONDS = 5.0
MAX_UDP_BYTES = 4096
RCODE_SERVFAIL = 2
RESOLVER_HEADER = re.compile(r"^resolver #\d+")
FIELD = re.compile(r"^\s+([a-z_ ]+?)(?:\[\d+\])?\s*:\s*(.+)$")

Server = tuple[str, int]


class DnsRule(BaseModel):
    """Names under `domain` are answered by `servers`."""

    domain: str
    servers: list[Server]


class DnsView(BaseModel):
    """How this Mac resolves names right now."""

    default_servers: list[Server] = Field(default_factory=list)
    rules: list[DnsRule] = Field(default_factory=list)
    search_domains: list[str] = Field(default_factory=list)

    def servers_for(self, name: str) -> list[Server]:
        """The servers for a name: the rule with the longest matching domain, else the default."""
        name = name.rstrip(".").lower()
        best: DnsRule | None = None
        for rule in self.rules:
            if name == rule.domain or name.endswith("." + rule.domain):
                if best is None or len(rule.domain) > len(best.domain):
                    best = rule
        return best.servers if best else self.default_servers


def parse_server(text: str, default_port: int = DNS_PORT) -> Server | None:
    """ "10.1.2.3", "10.1.2.3:5353" or "[fd00::1]:53" as (address, port)."""
    text = text.strip()
    if text.startswith("["):
        address, _, port = text[1:].partition("]")
        return (address, int(port.lstrip(":") or default_port))
    if text.count(":") == 1:
        address, port = text.split(":")
        return (address, int(port)) if port.isdigit() else None
    return (text, default_port) if text else None


def parse_scutil(output: str) -> DnsView:
    """Read `scutil --dns`: the default servers, per-domain servers and search domains."""
    # Only the first section describes how ordinary lookups are routed; the
    # "scoped queries" section that follows is per network interface.
    section = output.split("DNS configuration (for scoped queries)")[0]
    view = DnsView()
    for block in re.split(r"\n(?=resolver #\d+)", section):
        if not RESOLVER_HEADER.match(block.strip()):
            continue
        fields: dict[str, list[str]] = {}
        for line in block.splitlines()[1:]:
            if match := FIELD.match(line):
                fields.setdefault(match.group(1).strip(), []).append(match.group(2).strip())
        if "mdns" in " ".join(fields.get("options", [])):
            continue  # Bonjour (.local) is not a DNS server we can forward to
        port = int(fields["port"][0]) if fields.get("port", [""])[0].isdigit() else DNS_PORT
        servers = [s for s in (parse_server(n, port) for n in fields.get("nameserver", [])) if s]
        if not servers:
            continue
        domain = fields.get("domain", [""])[0].rstrip(".").lower()
        if domain:
            view.rules.append(DnsRule(domain=domain, servers=servers))
        elif not view.default_servers:
            view.default_servers = servers
            view.search_domains = [d.rstrip(".").lower() for d in fields.get("search domain", [])]
    return view


def question_name(packet: bytes) -> str | None:
    """The name a DNS query asks about, or None if the packet is not a well-formed query."""
    if len(packet) < 12 or struct.unpack("!H", packet[4:6])[0] < 1:
        return None
    labels, position = [], 12
    while True:
        if position >= len(packet):
            return None
        length = packet[position]
        if length == 0:
            break
        if length > 63 or position + 1 + length > len(packet):
            return None  # compression pointers do not occur in a question
        labels.append(packet[position + 1 : position + 1 + length].decode("ascii", "replace"))
        position += 1 + length
    return ".".join(labels).lower()


def servfail(query: bytes) -> bytes:
    """A "server failure" reply to `query`, so the asker fails fast instead of waiting."""
    flags = struct.unpack("!H", query[2:4])[0]
    reply_flags = 0x8000 | (flags & 0x7900) | 0x0080 | RCODE_SERVFAIL
    return query[:2] + struct.pack("!H", reply_flags) + query[4:6] + b"\x00" * 6 + query[12:]


class _Exchange(asyncio.DatagramProtocol):
    """One UDP question to one upstream server."""

    def __init__(self, query: bytes) -> None:
        self._query = query
        self.reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.sendto(self._query)  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if not self.reply.done() and data[:2] == self._query[:2]:
            self.reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)


async def ask_udp(server: Server, query: bytes, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(lambda: _Exchange(query), remote_addr=server)
    try:
        return await asyncio.wait_for(protocol.reply, timeout)
    finally:
        transport.close()


async def ask_tcp(server: Server, query: bytes, timeout: float) -> bytes:
    async def exchange() -> bytes:
        reader, writer = await asyncio.open_connection(*server)
        try:
            writer.write(struct.pack("!H", len(query)) + query)
            await writer.drain()
            (length,) = struct.unpack("!H", await reader.readexactly(2))
            return await reader.readexactly(length)
        finally:
            writer.close()

    return await asyncio.wait_for(exchange(), timeout)


class DnsForwarder:
    """Listens on host loopback and forwards each query to the right upstream server."""

    def __init__(
        self,
        port: int,
        extra_rules: dict[str, list[str]] | None = None,
        timeout: float = UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        self.port = port
        self._timeout = timeout
        self._extra_rules = [
            DnsRule(domain=domain.strip(".").lower(), servers=[s for s in map(parse_server, servers) if s])
            for domain, servers in (extra_rules or {}).items()
        ]
        self._view = DnsView()
        self._view_read_at = 0.0
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self.running = False

    async def view(self) -> DnsView:
        """The Mac's current DNS setup, re-read every few seconds: VPNs come and go."""
        if time.monotonic() - self._view_read_at > CONFIG_TTL_SECONDS:
            output = await run_command("scutil", "--dns") or ""
            parsed = parse_scutil(output)
            # Rules from config.yaml come first so they win over the system's for the same domain.
            parsed.rules = self._extra_rules + [
                rule for rule in parsed.rules if rule.domain not in {r.domain for r in self._extra_rules}
            ]
            self._view, self._view_read_at = parsed, time.monotonic()
        return self._view

    async def resolve(self, query: bytes, over_tcp: bool = False) -> bytes:
        name = question_name(query)
        if name is None:
            return servfail(query) if len(query) >= 12 else b""
        own = (LOOPBACK, self.port)
        servers = [server for server in (await self.view()).servers_for(name) if server != own]
        ask = ask_tcp if over_tcp else ask_udp
        for server in servers:
            try:
                return await ask(server, query, self._timeout)
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                continue  # this server did not answer: try the next one
        # Names are not logged: they can reveal what an agent or the user is working on.
        log.info("dns query failed", extra={"servers_tried": len(servers)})
        return servfail(query)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        forwarder = self

        class Listener(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                async def answer() -> None:
                    reply = await forwarder.resolve(data)
                    if reply:
                        self.transport.sendto(reply, addr)  # type: ignore[attr-defined]

                task = loop.create_task(answer())
                task.add_done_callback(lambda done: done.exception())

        async def on_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                (length,) = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 5))
                query = await asyncio.wait_for(reader.readexactly(length), 5)
                reply = await forwarder.resolve(query, over_tcp=True)
                writer.write(struct.pack("!H", len(reply)) + reply)
                await writer.drain()
            except (OSError, TimeoutError, asyncio.IncompleteReadError, struct.error):
                pass
            finally:
                writer.close()

        try:
            self._udp, _ = await loop.create_datagram_endpoint(Listener, local_addr=(LOOPBACK, self.port))
            self._tcp = await asyncio.start_server(on_tcp, LOOPBACK, self.port)
        except OSError as exc:
            # Agents then fall back to the VM's ordinary DNS; everything but internal names works.
            log.warning("dns forwarder not started", extra={"port": self.port, "error": str(exc)})
            self.stop()
            return
        self.running = True
        log.info("dns forwarder listening", extra={"port": self.port})

    def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
        if self._tcp is not None:
            self._tcp.close()
        self._udp = self._tcp = None
        self.running = False

    async def guest_settings(self) -> dict[str, Any] | None:
        """What an agent's computer needs to use this forwarder, or None if it is not running."""
        if not self.running:
            return None
        return {"port": self.port, "search_domains": (await self.view()).search_domains}
