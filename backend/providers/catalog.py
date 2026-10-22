"""Model IDs known for each provider, kept in the database.

Filled from what providers list, what passed a Test, what agents were given and
what finished a task; offered wherever a model ID is typed.
"""

from __future__ import annotations

from sqlmodel import Session, select

from backend.db.models import KnownModel, utcnow

MAX_PER_PROVIDER = 300


def remember(session: Session, provider: str, model: str, source: str, worked: bool = False) -> None:
    """Note a model ID for a provider (no commit: the caller's transaction carries it)."""
    model = model.strip()
    if not provider or not model or len(model) > 200:
        return
    row = session.exec(select(KnownModel).where(KnownModel.provider == provider, KnownModel.model == model)).first()
    now = utcnow()
    if row is None:
        row = KnownModel(provider=provider, model=model, source=source, seen_at=now)
    else:
        row.seen_at = now
        if source != "listed":
            row.source = source  # what the user did says more than a listing
    if worked:
        row.worked_at = now
    session.add(row)


def known(session: Session, provider: str) -> list[str]:
    """Model IDs for a provider: those that worked most recently first, then the rest by name."""
    rows = session.exec(select(KnownModel).where(KnownModel.provider == provider)).all()
    rows = sorted(rows, key=lambda row: (row.worked_at is None, -(row.worked_at.timestamp() if row.worked_at else 0), row.model))
    return [row.model for row in rows[:MAX_PER_PROVIDER]]
