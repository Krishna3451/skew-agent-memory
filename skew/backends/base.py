"""Backends differ ONLY in transaction discipline.

All three run against the same CockroachDB cluster and execute identical logic,
so the measured variable is isolation -- not storage engine, not model quality.
No strawman, no simulated competitor.

`remember(fact)` is the read-modify-write every agent-memory system performs on
ingest:

    1. look up the active belief for (subject, predicate)
    2. same object      -> reinforce it (observation_count += 1)
    3. different object -> supersede the old row, insert the new one
    4. nothing found    -> insert

Step 1 reads; steps 2-4 write based on what step 1 saw. If a peer commits in the
gap, the decision was made against stale state. That is the shape of Mem0 issue
#6515 (open: silent duplicate memories under concurrency).

Two deliberate design choices, both about keeping the experiment honest:

* **Identity is relational, never approximate.** The lookup is anchored on
  (run_id, backend, subject, predicate). An ANN search would be the wrong tool:
  it is approximate, so a near-miss silently becomes an INSERT, and the measured
  "anomaly" would really be embedding quality rather than a race. Cosine
  similarity is used for semantic *recall* (see recall.py), never for identity.

* **No embedding in the decision path.** Otherwise the benchmark measures the
  encoder. It measures transaction discipline instead.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg

from ..embed import to_pgvector


@dataclass
class Fact:
    subject: str
    predicate: str
    object: str
    content: str
    embedding: list[float]


@dataclass
class WriteResult:
    op: str
    memory_id: str | None
    retries: int
    latency_ms: float
    txn_ok: bool
    detail: str = ""


class Backend:
    name: str = "base"
    isolation: str | None = None
    use_locking: bool = False
    single_txn: bool = True

    def __init__(self, dsn: str, run_id: str):
        self.dsn = dsn
        self.run_id = run_id

    def _find_active(self, cur, f: Fact):
        """The active belief for this key. Relational, exact, index-backed.

        Putting (run_id, backend, subject, predicate) in the WHERE clause places
        a real predicate in the transaction's read set, so a concurrent insert
        into that group forces a 40001 retry under SERIALIZABLE. That is what
        makes the correct backend correct.
        """
        lock = " FOR UPDATE" if self.use_locking else ""
        cur.execute(
            f"""
            SELECT id, object, observation_count
              FROM memories
             WHERE run_id = %s AND backend = %s
               AND subject = %s AND predicate = %s
               AND status = 'active'
             ORDER BY created_at
             LIMIT 1{lock}
            """,
            (self.run_id, self.name, f.subject, f.predicate),
        )
        return cur.fetchone()

    def _apply(self, cur, f: Fact, found) -> tuple[str, str]:
        if found:
            mem_id, obj = found[0], found[1]
            if obj == f.object:
                cur.execute(
                    """UPDATE memories
                          SET observation_count = observation_count + 1,
                              confidence = least(confidence + 0.05, 1.0),
                              updated_at = now()
                        WHERE id = %s""",
                    (mem_id,),
                )
                return "reinforce", str(mem_id)
            cur.execute(
                "UPDATE memories SET status='superseded', updated_at=now() WHERE id=%s",
                (mem_id,),
            )
            new_id = self._insert(cur, f)
            cur.execute("UPDATE memories SET superseded_by=%s WHERE id=%s",
                        (new_id, mem_id))
            return "supersede", new_id
        return "insert", self._insert(cur, f)

    def _insert(self, cur, f: Fact) -> str:
        cur.execute(
            """INSERT INTO memories
                 (run_id, backend, subject, predicate, object, content, embedding)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (self.run_id, self.name, f.subject, f.predicate, f.object,
             f.content, to_pgvector(f.embedding)),
        )
        return str(cur.fetchone()[0])

    def remember(self, conn: psycopg.Connection, f: Fact) -> WriteResult:
        raise NotImplementedError

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, connect_timeout=30)
        conn.autocommit = self.single_txn is False
        return conn
