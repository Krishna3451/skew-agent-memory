# Judging evidence map

One place mapping each criterion to the code that backs it, so nothing has to be taken on trust.

---

## 1 · Agentic Memory Design

CockroachDB is not storage under an agent here — the memory layer *is* the project. The claim
is that an agent's memory is a **read-modify-write on contested state**, and that treating it as
anything less corrupts silently.

| Evidence | Where |
|---|---|
| Two-layer model: mutable beliefs + append-only audit of every attempt | [`db/schema.sql`](db/schema.sql) |
| The ingest operation (lookup → reinforce / supersede / insert) | [`skew/backends/base.py`](skew/backends/base.py) `_find_active`, `_apply` |
| Belief supersession with `superseded_by` lineage rather than deletion | `base.py` `_apply` |
| `observation_count` / `confidence` reinforcement on repeated evidence | `base.py` `_apply` |
| Relational identity vs. vector similarity — and why conflating them breaks | `base.py` module docstring |
| Semantic recall over the same rows, same transaction boundary | [`skew/recall.py`](skew/recall.py) |
| Read-your-writes: write in a txn, search with zero delay, find it | `recall.py` `read_your_writes` |

**Beyond toy queries:** the write path is a locking read plus up to three dependent writes inside
one serializable transaction, with `40001` retry handling and retry counts surfaced in the UI.

## 2 · Technological Implementation

| Evidence | Where |
|---|---|
| Three isolation disciplines, identical logic — the controlled variable | [`skew/backends/impls.py`](skew/backends/impls.py) |
| Serialization-failure retry with bounded exponential backoff | `impls.py` `Serializable.remember` |
| `SELECT … FOR UPDATE` row locking | `base.py` `_find_active` |
| Vector index created *with* the table (backfill on a populated table blocks writes) | `db/schema.sql` |
| Vector index prefix columns are equality-only — filters chosen accordingly | `db/schema.sql` comment |
| Concurrent workload runner, per-worker connections | [`skew/harness.py`](skew/harness.py) `run_backend` |
| Anomaly analysis computed from committed state, not from client bookkeeping | `harness.py` `analyse` |
| `EXPLAIN` output surfaced in the UI to prove the index is used | `recall.py` `explain_recall` |
| Embedding provider abstracted; Bedrock and local encoder share dimensions | [`skew/embed.py`](skew/embed.py) |

## 3 · Real-World Impact

The failure is live in the libraries people ship on today:

- **Mem0 #6515** — TOCTOU race in `add()` creating permanent duplicate memories. Open.
  *"There is no error, warning, or user-visible signal."*
- **Letta #3366** — `ConcurrentUpdateError` downgraded ERROR → WARNING.
- **LangGraph Store** — `put` overwrites, no optimistic concurrency control.
- **AWS AgentCore** — shipped `STRICTLY_CONSISTENT` metadata (May 2026) because scoping keys
  were LLM-inferred and non-deterministic.

In the recorded run the unsafe backends ended up holding `ben allergy = penicillin` **and**
`ben allergy = sulfa` as simultaneously active beliefs. An agent reading that memory has no way
to know which is true. See the highlighted rows in [`docs/screenshot.png`](docs/screenshot.png).

## 4 · Product Readiness

| Concern | How it is addressed |
|---|---|
| **Observability** | `memory_events` records every attempt — op, retries, latency, success — so anomalies are attributable after the fact |
| **Resilience** | Bounded retry budget; exhausted retries recorded as `conflict_abort` rather than silently dropped |
| **Correctness under load** | The benchmark itself is the regression test; anomaly counts must stay at zero |
| **Isolation** | Every run is scoped by `run_id`; concurrent runs cannot contaminate each other |
| **Failure surfacing** | Retry counts and p95 latency shown in the UI — the cost of correctness is displayed, not hidden |
| **Secrets** | Connection string via `.env`, git-ignored; no credentials in source |
| **Reproducibility** | Fixed workload seed; `Dockerfile` and pinned `requirements.txt` |

**Known limits, stated plainly:** CockroachDB Basic is single-region, so multi-region survival is
not demonstrated. `crdb_internal` is restricted on Basic, so cluster-level introspection is
unavailable. Neither is worked around or glossed over.

## 5 · Creativity & Originality

The field has saturated benchmarks for retrieval and new ones for temporal validity. For
**concurrent writes there is no dataset, no metric, no leaderboard** — while two 2026 surveys
name multi-agent memory the top open frontier. SKEW is a first attempt at that missing yardstick,
and the anomaly vocabulary is borrowed from database literature (lost update, write skew,
phantom) rather than invented.

The non-obvious result is documented rather than hidden: **an approximate vector search cannot
anchor a serializable transaction.** Our first implementation used `ORDER BY embedding <=> …
LIMIT 1` for dedup and serializable still scored 16 anomalies. Similarity ranks; it must never
decide identity. Write-up in [`README.md`](README.md#the-thing-we-got-wrong-first-and-it-is-the-interesting-part).

---

## Reproducing the headline numbers

```bash
python run_bench.py --reset --concurrency 24 --repeats 14
```

84 writes over 6 distinct facts. Expect ~18 anomalies at `no-txn`, ~26 at `read-committed`,
and **0** at `serializable`. Exact counts vary run to run — that variance is the point: the
unsafe backends are non-deterministic, the safe one is not.
