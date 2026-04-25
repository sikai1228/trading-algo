"""NewsMonitor: abstract contract for normalized news event streams."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class NewsMonitor(ABC):
    """Abstract source of normalized NewsEvent objects."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[Any]: ...
