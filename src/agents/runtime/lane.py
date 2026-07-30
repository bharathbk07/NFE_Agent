"""Session lane queue — serial PE agent turns per thread (OpenClaw-style)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict


class SessionLaneQueue:
    """One turn at a time per session key; optional global cap."""

    def __init__(self, *, global_limit: int = 8) -> None:
        self._lanes: Dict[str, asyncio.Lock] = {}
        self._global = asyncio.Semaphore(max(1, global_limit))
        self._meta = asyncio.Lock()

    async def _lane(self, session_key: str) -> asyncio.Lock:
        async with self._meta:
            if session_key not in self._lanes:
                self._lanes[session_key] = asyncio.Lock()
            return self._lanes[session_key]

    @asynccontextmanager
    async def acquire(self, session_key: str) -> AsyncIterator[None]:
        lane = await self._lane(session_key or "default")
        async with self._global:
            async with lane:
                yield


session_lanes = SessionLaneQueue()
