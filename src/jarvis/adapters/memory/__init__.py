"""Хранилище в памяти: те же порты, что и у SQLite (M2). Используется тестами и eval в M1."""

from jarvis.adapters.memory.storage import InMemoryIdAllocator, InMemoryStorage, InMemoryUnitOfWork

__all__ = ["InMemoryIdAllocator", "InMemoryStorage", "InMemoryUnitOfWork"]
