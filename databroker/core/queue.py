"""
databroker.core.queue -- async job queue connecting the pipeline stages.

One Protocol, two implementations:
  InMemoryQueue  -- asyncio.Queue, for tests and single-process dev runs.
  SqliteQueue    -- durable, survives restarts, safe for a few worker processes
                    on one box (the solo-founder default). Swap for Redis/Postgres
                    later by writing another class with the same 4 methods.

Items are plain JSON-able dicts. get() leases an item; the worker calls ack() on
success or nack() to requeue. This gives at-least-once delivery so a crash
mid-removal doesn't silently drop the job.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional, Protocol


class Queue(Protocol):
    async def put(self, item: dict) -> None: ...
    async def get(self, timeout: float = 5.0) -> Optional[dict]: ...
    async def ack(self, lease: object) -> None: ...
    async def nack(self, lease: object) -> None: ...
    async def size(self) -> int: ...


# in-memory
class InMemoryQueue:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    async def put(self, item: dict) -> None:
        await self._q.put(item)

    async def get(self, timeout: float = 5.0) -> Optional[dict]:
        try:
            item = await asyncio.wait_for(self._q.get(), timeout)
            return item
        except asyncio.TimeoutError:
            return None

    async def ack(self, lease) -> None:
        self._q.task_done()

    async def nack(self, lease) -> None:
        # re-enqueue the item carried on the lease
        if lease is not None:
            await self._q.put(lease)
        self._q.task_done()

    async def size(self) -> int:
        return self._q.qsize()


# durable sqlite --
class SqliteQueue:
    """Durable FIFO with leasing. Each named queue is a row-filtered view of one table.
    sqlite calls run in a thread so they don't block the event loop."""

    def __init__(self, db_path: str | Path, name: str, lease_seconds: int = 600):
        self.db_path = str(db_path)
        self.name = name
        self.lease_seconds = lease_seconds
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue TEXT NOT NULL,
                payload TEXT NOT NULL,
                leased_until REAL DEFAULT 0,
                done INTEGER DEFAULT 0,
                created REAL NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_jobs_q ON jobs(queue, done, leased_until)")

    # sync core, wrapped in to_thread below
    def _put(self, item):
        with self._conn() as c:
            c.execute("INSERT INTO jobs(queue,payload,created) VALUES(?,?,?)",
                      (self.name, json.dumps(item), time.time()))

    def _get(self):
        now = time.time()
        with self._conn() as c:
            # atomic claim: a single UPDATE...RETURNING holds the write lock for the
            # whole statement, so two workers can't lease the same row.
            cur = c.execute(
                """UPDATE jobs SET leased_until=?
                   WHERE id=(SELECT id FROM jobs
                             WHERE queue=? AND done=0 AND leased_until<?
                             ORDER BY id LIMIT 1)
                   RETURNING id, payload""",
                (now + self.lease_seconds, self.name, now))
            row = cur.fetchone()
            if not row:
                return None
            job_id, payload = row
            return {"_lease_id": job_id, **json.loads(payload)}

    def _ack(self, job_id):
        with self._conn() as c:
            c.execute("UPDATE jobs SET done=1 WHERE id=?", (job_id,))

    def _nack(self, job_id):
        with self._conn() as c:
            c.execute("UPDATE jobs SET leased_until=0 WHERE id=?", (job_id,))

    def _size(self):
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM jobs WHERE queue=? AND done=0",
                             (self.name,)).fetchone()[0]

    async def put(self, item: dict) -> None:
        await asyncio.to_thread(self._put, item)

    async def get(self, timeout: float = 5.0) -> Optional[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = await asyncio.to_thread(self._get)
            if item is not None:
                return item
            await asyncio.sleep(0.4)
        return None

    async def ack(self, lease) -> None:
        job_id = lease.get("_lease_id") if isinstance(lease, dict) else lease
        if job_id is not None:
            await asyncio.to_thread(self._ack, job_id)

    async def nack(self, lease) -> None:
        job_id = lease.get("_lease_id") if isinstance(lease, dict) else lease
        if job_id is not None:
            await asyncio.to_thread(self._nack, job_id)

    async def size(self) -> int:
        return await asyncio.to_thread(self._size)
