from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущее время с часовым поясом (UTC)."""
        ...

    def monotonic(self) -> float:
        """Монотонные секунды для измерения длительностей."""
        ...
