"""Shared channels: a common place where agents and the user write to each other."""

from __future__ import annotations

import re

from sqlalchemy import Engine
from sqlmodel import Session, select

from backend.db.models import Channel, ChannelMessage

CHANNEL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
DEFAULT_CHANNEL = "general"
MAX_MESSAGE_CHARS = 8000
HUMAN_AUTHOR = "you"
SYSTEM_AUTHOR = "system"
MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9-]{0,62})")


class ChannelError(Exception):
    pass


def mentions(content: str) -> list[str]:
    """Agent names mentioned as @name, in order, without duplicates."""
    return list(dict.fromkeys(MENTION.findall(content)))


class ChannelService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_default(self) -> None:
        with Session(self._engine) as session:
            if session.exec(select(Channel)).first() is None:
                session.add(Channel(name=DEFAULT_CHANNEL))
                session.commit()

    def list(self) -> list[Channel]:
        with Session(self._engine) as session:
            return list(session.exec(select(Channel).order_by(Channel.id)).all())  # type: ignore[arg-type]

    def create(self, name: str) -> Channel:
        if not re.match(CHANNEL_NAME_PATTERN, name):
            raise ChannelError("Channel names use lowercase letters, digits and dashes.")
        with Session(self._engine) as session:
            if session.exec(select(Channel).where(Channel.name == name)).first() is not None:
                raise ChannelError(f"#{name} already exists.")
            channel = Channel(name=name)
            session.add(channel)
            session.commit()
            session.refresh(channel)
            return channel

    def get(self, channel_id: int) -> Channel | None:
        with Session(self._engine) as session:
            return session.get(Channel, channel_id)

    def find(self, name: str) -> Channel | None:
        with Session(self._engine) as session:
            return session.exec(select(Channel).where(Channel.name == name.lstrip("#"))).first()

    def messages(self, channel_id: int, after_id: int = 0, limit: int = 200) -> list[ChannelMessage]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(ChannelMessage)
                .where(ChannelMessage.channel_id == channel_id, ChannelMessage.id > after_id)  # type: ignore[operator]
                .order_by(ChannelMessage.id.desc())  # type: ignore[union-attr]
                .limit(limit)
            ).all()
            return list(reversed(rows))

    def post(self, channel_id: int, author: str, content: str) -> ChannelMessage:
        content = content.strip()
        if not content:
            raise ChannelError("A message cannot be empty.")
        with Session(self._engine) as session:
            message = ChannelMessage(
                channel_id=channel_id, author=author, content=content[:MAX_MESSAGE_CHARS]
            )
            session.add(message)
            session.commit()
            session.refresh(message)
            return message
