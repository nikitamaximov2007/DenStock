"""Fail-closed per-destination byte budgeting for backup generations.

Adapters must report *physical* namespace usage, including old versions, trash
and partial uploads. An adapter that cannot make that promise is not eligible
for automatic rotation under the exact-byte contract.
"""

from dataclasses import dataclass
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
    """Rotate oldest verified generations, preserving the newest known-good.

    The adapter remeasures after *each* deletion. Hidden versions, failed
    deletion, concurrent additions and partials can therefore never be assumed
    to have released capacity. No upload is permitted unless the final measure
    plus the entire incoming generation fits the hard limit.
    """
    if incoming_bytes < 0 or limit <= 0 or incoming_bytes > limit:
        raise BudgetError("Новая копия превышает лимит хранилища.")
    current = destination.physical_bytes()
    if current < 0:
        raise BudgetError("Не удалось достоверно измерить хранилище.")
    if current + incoming_bytes <= limit:
        return []
    generations = destination.generations()
    verified = sorted((g for g in generations if g.verified), key=lambda g: g.name)
    if not verified:
        raise BudgetError("Нет проверенного поколения: автоматическое удаление запрещено.")
    removed: list[str] = []
    for generation in verified[:-1]:
        if current + incoming_bytes <= limit:
            break
        destination.delete_generation(generation.name)
        after = destination.physical_bytes()
        if after < 0 or after >= current:
            raise BudgetError("Удаление не освободило подтверждённый объём; остановка.")
        removed.append(generation.name)
        current = after
    if current + incoming_bytes > limit:
        raise BudgetError("Нельзя освободить место без удаления последней исправной копии.")
    return removed
