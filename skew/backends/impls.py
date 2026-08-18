"""The three disciplines."""
from __future__ import annotations

import time

import psycopg

from .base import Backend, Fact, WriteResult

MAX_RETRIES = 8


class NoTransaction(Backend):
    """What most agent-memory frameworks actually do.

    Read in one statement, decide in Python, write in another -- each statement
    its own implicit transaction. Nothing prevents a peer from committing in the
    gap. This is the shape of Mem0 #6515.
    """

    name = "no-txn"
    single_txn = False        # autocommit: every statement commits independently
    use_locking = False

    def remember(self, conn, f: Fact) -> WriteResult:
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            found = self._find_active(cur, f)
            # --- the window. Another worker can commit right here. ---
            op, mem_id = self._apply(cur, f, found)
        return WriteResult(op, mem_id, 0, (time.perf_counter() - t0) * 1000, True)


class ReadCommitted(Backend):
    """One transaction, but the weaker isolation level.

    Wrapping the read and the write in a single READ COMMITTED transaction feels
    safe and is what a developer coming from PostgreSQL defaults would write. It
    still permits the anomaly, because the read takes a fresh snapshot per
    statement and nothing is locked.
    """

    name = "read-committed"
    isolation = "read committed"
    single_txn = True
    use_locking = False

    def remember(self, conn, f: Fact) -> WriteResult:
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            found = self._find_active(cur, f)
            op, mem_id = self._apply(cur, f, found)
        conn.commit()
        return WriteResult(op, mem_id, 0, (time.perf_counter() - t0) * 1000, True)


class Serializable(Backend):
    """CockroachDB's default, plus explicit row locking and a retry loop.

    The read and the write are one serializable transaction. Concurrent writers
    that would produce a non-serializable outcome are aborted with SQLSTATE 40001
    and retried, so the anomaly cannot be committed. Retries are counted and
    surfaced -- they are the visible cost of correctness, not an error.
    """

    name = "serializable"
    isolation = "serializable"
    single_txn = True
    use_locking = True

    def remember(self, conn, f: Fact) -> WriteResult:
        t0 = time.perf_counter()
        retries = 0
        while True:
            try:
                with conn.cursor() as cur:
                    found = self._find_active(cur, f)
                    op, mem_id = self._apply(cur, f, found)
                conn.commit()
                return WriteResult(op, mem_id, retries,
                                   (time.perf_counter() - t0) * 1000, True)
            except psycopg.errors.SerializationFailure:
                conn.rollback()
                retries += 1
                if retries > MAX_RETRIES:
                    return WriteResult("conflict_abort", None, retries,
                                       (time.perf_counter() - t0) * 1000, False,
                                       "retry budget exhausted")
                time.sleep(min(0.005 * (2 ** retries), 0.4))
            except Exception as e:
                conn.rollback()
                return WriteResult("error", None, retries,
                                   (time.perf_counter() - t0) * 1000, False, str(e)[:200])


ALL = [NoTransaction, ReadCommitted, Serializable]
