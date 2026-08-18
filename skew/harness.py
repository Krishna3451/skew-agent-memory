"""Concurrent workload runner and anomaly analyser."""
from __future__ import annotations

import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import psycopg

from .backends.base import Fact
from .embed import to_pgvector

# Facts an agent might learn about a user. Each has a stable "same fact"
# paraphrase set plus a contradicting update, which is how real ingest looks.
SEED = [
    ("user:ana", "works_at", "Northwind",
     ["Ana works at Northwind", "Ana is employed by Northwind",
      "Ana's employer is Northwind"], "Contoso"),
    ("user:ana", "city", "Lisbon",
     ["Ana lives in Lisbon", "Ana is based in Lisbon",
      "Ana's home city is Lisbon"], "Porto"),
    ("user:ben", "allergy", "penicillin",
     ["Ben is allergic to penicillin", "Ben has a penicillin allergy",
      "penicillin causes Ben a reaction"], "sulfa"),
    ("user:ben", "role", "staff engineer",
     ["Ben is a staff engineer", "Ben works as a staff engineer",
      "Ben's title is staff engineer"], "principal engineer"),
    ("user:cleo", "timezone", "CET",
     ["Cleo is in CET", "Cleo's timezone is CET", "Cleo works on CET time"], "GMT"),
    ("user:cleo", "prefers", "email",
     ["Cleo prefers email", "Cleo likes to be contacted by email",
      "email is Cleo's preferred channel"], "slack"),
]


@dataclass
class Workload:
    ops: list[Fact]
    expected_keys: int          # distinct (subject, predicate) pairs touched
    total_ops: int


def build_workload(encoder, repeats: int = 6, contradiction_rate: float = 0.25,
                   seed: int = 7) -> Workload:
    """Each fact is written `repeats` times concurrently, some as contradictions.

    Correct behaviour: exactly one active row per (subject, predicate), whose
    observation_count reflects every reinforcing write.
    """
    rng = random.Random(seed)
    ops: list[Fact] = []
    for subj, pred, obj, paraphrases, contra in SEED:
        for i in range(repeats):
            if rng.random() < contradiction_rate and i > 0:
                text = f"{subj.split(':')[1]} {pred.replace('_',' ')} {contra}"
                ops.append(Fact(subj, pred, contra, text, encoder.encode(text)))
            else:
                text = paraphrases[i % len(paraphrases)]
                ops.append(Fact(subj, pred, obj, text, encoder.encode(text)))
    rng.shuffle(ops)
    return Workload(ops, len(SEED), len(ops))


def run_backend(backend_cls, dsn: str, run_id: str, wl: Workload,
                concurrency: int = 12, on_event=None) -> dict:
    """Fire the whole workload at one backend with `concurrency` workers."""
    be = backend_cls(dsn, run_id)
    results = []

    def worker(idx: int, chunk: list[Fact]):
        conn = be.connect()
        local = []
        try:
            for f in chunk:
                r = be.remember(conn, f)
                local.append(r)
                if on_event:
                    on_event(be.name, idx, r)
        finally:
            conn.close()
        return local

    chunks: list[list[Fact]] = [[] for _ in range(concurrency)]
    for i, f in enumerate(wl.ops):
        chunks[i % concurrency].append(f)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for out in ex.map(lambda p: worker(*p), list(enumerate(chunks))):
            results.extend(out)

    _record_events(dsn, run_id, be.name, results)
    return {
        "backend": be.name,
        "isolation": be.isolation or "autocommit (none)",
        "ops": len(results),
        "retries": sum(r.retries for r in results),
        "failures": sum(0 if r.txn_ok else 1 for r in results),
        "p50_ms": _pct([r.latency_ms for r in results], 50),
        "p95_ms": _pct([r.latency_ms for r in results], 95),
    }


def _pct(xs: list[float], p: int) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(len(s) * p / 100))], 1)


def _record_events(dsn, run_id, backend, results):
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        cur.executemany(
            """INSERT INTO memory_events
                 (run_id, backend, worker, op, memory_id, retries, latency_ms, txn_ok, detail)
               VALUES (%s,%s,0,%s,%s,%s,%s,%s,%s)""",
            [(run_id, backend, r.op, r.memory_id, r.retries,
              r.latency_ms, r.txn_ok, r.detail) for r in results],
        )


def analyse(dsn: str, run_id: str, backend: str, wl: Workload) -> dict:
    """Count the anomalies the literature names, from committed state."""
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        # Duplicate rows: more than one ACTIVE row for the same (subject,predicate,object).
        # Correct behaviour reinforces the existing row instead of inserting a twin.
        cur.execute(
            """SELECT coalesce(sum(n - 1), 0) FROM (
                   SELECT count(*) AS n FROM memories
                    WHERE run_id=%s AND backend=%s AND status='active'
                    GROUP BY subject, predicate, object) t
               WHERE n > 1""", (run_id, backend))
        duplicates = int(cur.fetchone()[0])

        # Contradiction survival: two different beliefs both active for one key.
        cur.execute(
            """SELECT count(*) FROM (
                   SELECT subject, predicate FROM memories
                    WHERE run_id=%s AND backend=%s AND status='active'
                    GROUP BY subject, predicate
                   HAVING count(DISTINCT object) > 1) t""", (run_id, backend))
        live_contradictions = int(cur.fetchone()[0])

        # Lost reinforcements: every successful write should be reflected exactly
        # once, either as a new row or as an increment on an existing one.
        cur.execute(
            """SELECT coalesce(sum(observation_count),0) FROM memories
                WHERE run_id=%s AND backend=%s""", (run_id, backend))
        observed = int(cur.fetchone()[0])

        cur.execute(
            """SELECT count(*) FROM memory_events
                WHERE run_id=%s AND backend=%s AND txn_ok AND op<>'error'""",
            (run_id, backend))
        committed_writes = int(cur.fetchone()[0])

        cur.execute(
            """SELECT count(*) FROM memories
                WHERE run_id=%s AND backend=%s AND status='active'""", (run_id, backend))
        active_rows = int(cur.fetchone()[0])

    return {
        "duplicates": duplicates,
        "live_contradictions": live_contradictions,
        "lost_reinforcements": max(0, committed_writes - observed),
        "active_rows": active_rows,
        "expected_active_rows": wl.expected_keys,
        "excess_rows": max(0, active_rows - wl.expected_keys),
        "total_anomalies": duplicates + live_contradictions
                           + max(0, committed_writes - observed),
    }
