"""Allocation of the host loopback port each agent VM forwards to its daemon."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable

from backend.vm.errors import VMError

LOOPBACK = "127.0.0.1"
DAEMON_PORTS = range(18000, 19000)

PortCheck = Callable[[int], bool]


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def allocate_daemon_port(taken: Iterable[int], is_free: PortCheck = is_port_free) -> int:
    taken = set(taken)
    for port in DAEMON_PORTS:
        if port not in taken and is_free(port):
            return port
    raise VMError("no free guest daemon port available")

