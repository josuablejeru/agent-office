"""Builds the QEMU command line for an agent VM. Pure functions, no side effects."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from backend.vm.capabilities import QEMU_SYSTEM_BINARY
from backend.vm.ports import LOOPBACK

GUEST_DAEMON_PORT = 8765


class VMSpec(BaseModel):
    """Everything needed to launch one agent VM."""

    name: str
    disk_path: Path
    firmware_path: Path
    memory_mb: int = Field(ge=512)
    cpus: int = Field(ge=1)
    # Host loopback port forwarded to the guest daemon.
    daemon_host_port: int = Field(ge=1024, le=65535)
    # VNC is served on a unix socket: unlike a loopback TCP port, guests cannot reach it.
    vnc_socket_path: Path
    qmp_socket_path: Path
    pid_file_path: Path
    console_log_path: Path
    # cloud-init NoCloud seed, attached read-only.
    seed_iso_path: Path | None = None


def escape_option(value: Path | str) -> str:
    """Escape a value embedded in a comma-separated QEMU option string."""
    return str(value).replace(",", ",,")


def build_qemu_args(spec: VMSpec, qemu_binary: str = QEMU_SYSTEM_BINARY) -> list[str]:
    """Return the argv for a daemonized, HVF-accelerated ARM64 guest.

    The guest daemon is only reachable through host loopback; VNC and QMP use
    unix sockets.
    """
    args = [
        qemu_binary,
        "-name", spec.name,
        "-machine", "virt",
        "-accel", "hvf",
        "-cpu", "host",
        "-smp", str(spec.cpus),
        "-m", str(spec.memory_mb),
        "-bios", str(spec.firmware_path),
        "-drive", f"if=none,id=hd0,file={escape_option(spec.disk_path)},format=qcow2",
        "-device", "virtio-blk-pci,drive=hd0,bootindex=0",
    ]
    if spec.seed_iso_path is not None:
        args += [
            "-drive",
            f"if=none,id=seed,file={escape_option(spec.seed_iso_path)},format=raw,readonly=on",
            "-device", "virtio-blk-pci,drive=seed",
        ]
    args += [
        "-netdev", f"user,id=net0,hostfwd=tcp:{LOOPBACK}:{spec.daemon_host_port}-:{GUEST_DAEMON_PORT}",
        "-device", "virtio-net-pci,netdev=net0",
        "-device", "virtio-gpu-pci",
        "-device", "qemu-xhci",
        "-device", "usb-kbd",
        "-device", "usb-tablet",
        "-display", "none",
        "-vnc", f"unix:{escape_option(spec.vnc_socket_path)}",
        "-chardev", f"file,id=console,path={escape_option(spec.console_log_path)},append=on",
        "-serial", "chardev:console",
        "-qmp", f"unix:{escape_option(spec.qmp_socket_path)},server=on,wait=off",
        "-pidfile", str(spec.pid_file_path),
        "-daemonize",
    ]
    return args
