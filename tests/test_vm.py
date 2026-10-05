from __future__ import annotations

import asyncio
from pathlib import Path

from backend.vm.capabilities import detect_capabilities
from backend.vm.images import build_overlay_command
from backend.vm.qemu import VMSpec, build_qemu_args


def make_spec(tmp_path: Path) -> VMSpec:
    return VMSpec(
        name="research-agent",
        disk_path=tmp_path / "disk.qcow2",
        firmware_path=tmp_path / "edk2-aarch64-code.fd",
        memory_mb=4096,
        cpus=4,
        daemon_host_port=18765,
        vnc_socket_path=tmp_path / "vnc.sock",
        qmp_socket_path=tmp_path / "qmp.sock",
        pid_file_path=tmp_path / "qemu.pid",
        console_log_path=tmp_path / "console.log",
    )


def option(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == flag]


def test_qemu_args_use_hvf_and_virtio(tmp_path: Path) -> None:
    args = build_qemu_args(make_spec(tmp_path))
    assert args[0] == "qemu-system-aarch64"
    assert option(args, "-machine") == ["virt"]
    assert option(args, "-accel") == ["hvf"]
    assert option(args, "-cpu") == ["host"]
    assert option(args, "-m") == ["4096"]
    assert option(args, "-smp") == ["4"]
    assert f"if=none,id=hd0,file={tmp_path / 'disk.qcow2'},format=qcow2" in args
    assert "virtio-blk-pci,drive=hd0,bootindex=0" in args
    assert "virtio-net-pci,netdev=net0" in args


def test_qemu_args_bind_only_to_loopback(tmp_path: Path) -> None:
    args = build_qemu_args(make_spec(tmp_path))
    assert option(args, "-vnc") == [f"unix:{tmp_path / 'vnc.sock'}"]
    assert option(args, "-netdev") == ["user,id=net0,hostfwd=tcp:127.0.0.1:18765-:8765"]
    assert "0.0.0.0" not in " ".join(args)


def test_overlay_command_declares_backing_format(tmp_path: Path) -> None:
    command = build_overlay_command(tmp_path / "base.qcow2", tmp_path / "disk.qcow2")
    assert command == [
        "qemu-img", "create", "-f", "qcow2",
        "-b", str(tmp_path / "base.qcow2"), "-F", "qcow2",
        str(tmp_path / "disk.qcow2"),
    ]


def test_capabilities_report_missing_qemu() -> None:
    async def run(*args: str, timeout: float = 5.0) -> str | None:
        return "1" if args[0] == "sysctl" else None

    caps = asyncio.run(
        detect_capabilities(which=lambda _: None, run=run, system="Darwin", machine="arm64")
    )
    assert caps.apple_silicon and caps.hvf_supported
    assert caps.qemu_system_path is None and not caps.hvf_accel_available
    assert not caps.ready
    assert any("brew install qemu" in problem for problem in caps.problems)


def test_capabilities_ready_when_everything_present(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "qemu-system-aarch64"
    firmware = tmp_path / "share" / "qemu" / "edk2-aarch64-code.fd"
    for path in (binary, firmware):
        path.parent.mkdir(parents=True)
        path.touch()

    async def run(*args: str, timeout: float = 5.0) -> str | None:
        if args[0] == "sysctl":
            return "1"
        if args[1:] == ("--version",):
            return "QEMU emulator version 10.0.0\nCopyright"
        return "Accelerators supported in QEMU binary:\nhvf\ntcg"

    caps = asyncio.run(
        detect_capabilities(
            which=lambda name: str(tmp_path / "bin" / name),
            run=run,
            system="Darwin",
            machine="arm64",
        )
    )
    assert caps.ready and caps.problems == []
    assert caps.qemu_version == "QEMU emulator version 10.0.0"
    assert caps.firmware_path == str(firmware.resolve())


def test_capabilities_reject_non_apple_silicon() -> None:
    async def run(*args: str, timeout: float = 5.0) -> str | None:
        return None

    caps = asyncio.run(
        detect_capabilities(which=lambda _: None, run=run, system="Linux", machine="x86_64")
    )
    assert not caps.apple_silicon and not caps.ready


def test_qemu_args_attach_seed_read_only(tmp_path: Path) -> None:
    spec = make_spec(tmp_path).model_copy(update={"seed_iso_path": tmp_path / "seed.iso"})
    args = build_qemu_args(spec)
    assert f"if=none,id=seed,file={tmp_path / 'seed.iso'},format=raw,readonly=on" in args
    assert "id=seed" not in " ".join(build_qemu_args(make_spec(tmp_path)))


def test_qemu_args_escape_commas_in_paths(tmp_path: Path) -> None:
    spec = make_spec(tmp_path).model_copy(update={"disk_path": Path("/data/a,b/disk.qcow2")})
    assert "if=none,id=hd0,file=/data/a,,b/disk.qcow2,format=qcow2" in build_qemu_args(spec)
