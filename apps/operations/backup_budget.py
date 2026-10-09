"""Conservative byte preflight, not a provider-enforced physical quota."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

BYTE_LIMIT = 1_000_000_000


class BudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Generation:
    name: str
    verified: bool


class Destination(Protocol):
    def physical_bytes(self) -> int: ...

    def generations(self) -> list[Generation]: ...

    def delete_generation(self, name: str) -> None: ...


def reserve_capacity(
    destination: Destination,
    incoming_bytes: int,
    *,
    limit: int = BYTE_LIMIT,
) -> list[str]:
    """Refuse if the replacement cannot coexist with every retained generation.

    A preflight cannot prove a provider's hard quota. In particular it must
    never delete old recovery evidence merely to make a later upload possible.
    """
    if incoming_bytes < 0 or limit <= 0 or incoming_bytes > limit:
        raise BudgetError("Новая копия превышает лимит хранилища.")
    current = destination.physical_bytes()
    if current < 0:
        raise BudgetError("Не удалось достоверно измерить хранилище.")
    if current + incoming_bytes > limit:
        raise BudgetError("Новая копия не помещается без удаления проверенных поколений.")
    return []


def parse_signed_time(value) -> datetime | None:
    """A timezone-aware ISO timestamp from a signed receipt, else None."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def prune_superseded(destination, newest: str) -> list[str]:
    """Delete verified generations whose SIGNED creation time is older than ``newest``.

    Runs only after ``newest`` itself is uploaded/installed.  It is re-verified
    here, immediately before any deletion, so the newest independently
    verified recoverable generation always survives.  Generations that are
    unverified, incomplete, ambiguous or have no signed time are never touched:
    a listing that omits a file is not evidence that a generation is garbage.
    Nothing is ever deleted to make room *before* an upload.
    """
    if not destination.is_verified(newest):
        raise BudgetError("Новое поколение не подтверждено; старые копии не удаляются.")
    newest_time = destination.signed_created_at(newest)
    if newest_time is None:
        raise BudgetError("Нет подписанного времени нового поколения; удаление запрещено.")
    removed = []
    for generation in sorted(destination.generations(), key=lambda g: g.name):
        if generation.name == newest or not generation.verified:
            continue
        created = destination.signed_created_at(generation.name)
        if created is None or created >= newest_time:
            continue
        destination.delete_generation(generation.name)
        removed.append(generation.name)
    return removed
