"""Application settings and on-disk layout under the data directory."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

HOME_ENV_VAR = "AGENT_OFFICE_HOME"
XDG_CONFIG_ENV_VAR = "XDG_CONFIG_HOME"
APP_DIRNAME = "agent-office"

# sockaddr_un.sun_path on macOS holds 104 bytes including the terminator.
MAX_UNIX_SOCKET_PATH = 103
SHORT_TMP_DIR = Path("/tmp")

DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "openai": {"type": "openai", "api_key_env": "OPENAI_API_KEY"},
        "anthropic": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "grok": {
            "type": "openai-compatible",
            "base_url": "https://api.x.ai/v1",
            "api_key_env": "XAI_API_KEY",
        },
        "ollama": {
            "type": "openai-compatible",
            "base_url": "http://localhost:11434/v1",
            "api_key": "none",
            "context_chars": 60000,
        },
        # Google Cloud Vertex AI. Both need a project and Application Default
        # Credentials (`gcloud auth application-default login`), not an API key.
        "vertex": {"type": "vertex", "project": "", "region": "global"},
        "vertex-claude": {"type": "vertex-anthropic", "project": "", "region": "global"},
    },
    "app": {
        # The desktop app powers agent VMs off when it quits unless this is true.
        "keep_vms_running_on_quit": False,
    },
    "policy": {
        # What happens to tool calls no hardcoded rule covers: allow | require_approval
        "default_action": "allow",
    },
    # Optional decision provider for ambiguous tool calls (see README). Example:
    #   jev: {base_url: "https://...", api_key_env: JEV_API_KEY}
    "limits": {
        "max_tool_calls_per_run": 50,
        "max_runtime_seconds": 900,
        "max_repeated_identical_calls": 3,
    },
}


def merge_config(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay `overrides` on `defaults` without modifying either."""
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


class ProviderSettings(BaseModel):
    """One entry of the `providers` block in config.yaml."""

    type: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    # Output token limit per model response, for providers that require one.
    max_tokens: int | None = None
    # Rough size of the model's context in characters. When set, old tool results
    # are shortened to stay inside it (useful for small local models).
    context_chars: int | None = None
    # Google Cloud project and region, for the Vertex AI provider types.
    project: str | None = None
    region: str | None = None
    # Extra fields merged into every request body (provider-specific switches).
    extra_body: dict[str, Any] | None = None


class Settings(BaseModel):
    """Resolved paths for the app directory (default ~/.config/agent-office)."""

    home: Path

    @classmethod
    def from_env(cls) -> Settings:
        override = os.environ.get(HOME_ENV_VAR)
        if override:
            return cls(home=Path(override).expanduser())
        config_root = os.environ.get(XDG_CONFIG_ENV_VAR) or str(Path.home() / ".config")
        return cls(home=Path(config_root).expanduser() / APP_DIRNAME)

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"

    @property
    def db_path(self) -> Path:
        return self.home / "agent-office.db"

    @property
    def images_dir(self) -> Path:
        return self.home / "images"

    @property
    def agents_dir(self) -> Path:
        return self.home / "agents"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    def agent_dir(self, name: str) -> Path:
        return self.agents_dir / name

    @property
    def api_token_path(self) -> Path:
        return self.secrets_dir / "api-token"

    def load_api_token(self) -> str:
        """Token every API request must carry, generated on first use.

        Guest VMs can reach host loopback through QEMU's user networking, so
        listening on 127.0.0.1 alone does not keep an agent away from this API.
        """
        path = self.api_token_path
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
        return path.read_text().strip()

    def screenshots_dir(self, name: str) -> Path:
        return self.agent_dir(name) / "screenshots"

    def guest_secret_path(self, name: str) -> Path:
        return self.secrets_dir / f"{name}.guest-secret"

    def _socket_path(self, filename: str) -> Path:
        """A control socket path, kept short enough for a unix socket address."""
        preferred = self.home / "run" / filename
        if len(os.fsencode(preferred)) <= MAX_UNIX_SOCKET_PATH:
            return preferred
        digest = hashlib.sha1(os.fsencode(self.home)).hexdigest()[:10]
        return SHORT_TMP_DIR / f"agent-office-{os.getuid()}" / digest / filename

    def qmp_socket_path(self, agent_id: int) -> Path:
        return self._socket_path(f"{agent_id}.qmp")

    def vnc_socket_path(self, agent_id: int) -> Path:
        return self._socket_path(f"{agent_id}.vnc")

    def base_image_path(self, image: str) -> Path:
        return self.images_dir / f"{image}.qcow2"

    def ensure_layout(self) -> None:
        """Create the data directory tree and a default config.yaml if missing."""
        for directory in (self.home, self.images_dir, self.agents_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(mode=0o700, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))

    def load_user_config(self) -> dict[str, Any]:
        """config.yaml exactly as the user has it."""
        if not self.config_path.exists():
            return {}
        return yaml.safe_load(self.config_path.read_text()) or {}

    def load_config(self) -> dict[str, Any]:
        """The user's config laid over the defaults, so new settings need no file edit."""
        return merge_config(DEFAULT_CONFIG, self.load_user_config())

    def update_user_config(self, path: list[str], value: Any) -> None:
        """Set one value in config.yaml, leaving everything else as the user wrote it."""
        config = self.load_user_config()
        node = config
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[path[-1]] = value
        # Written beside the file and swapped in, so a crash cannot leave it half-written.
        temporary = self.config_path.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(config, sort_keys=False))
        os.replace(temporary, self.config_path)

    def load_providers(self) -> dict[str, ProviderSettings]:
        raw = self.load_config().get("providers") or {}
        # A provider set to null in config.yaml is hidden, built-in ones included.
        return {
            name: ProviderSettings.model_validate(entry)
            for name, entry in raw.items()
            if entry is not None
        }
