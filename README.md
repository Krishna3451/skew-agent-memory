# SKEW — agent memory under concurrency

**Every agent-memory system is benchmarked on recall. None is benchmarked on whether it stays
*correct* when several agents write at once.** SKEW is that benchmark, plus a CockroachDB
implementation that passes it.

![SKEW dashboard](docs/screenshot.png)

**Live demo:** _see submission_ · **Run it yourself:** `python run_bench.py --reset`

---

## The result

84 concurrent writes describing **6 distinct facts**. Correct behaviour is 6 active rows —
one per fact. Identical code, identical database, identical workload; the only variable is
transaction discipline.

| backend | isolation | duplicate rows | live contradictions | rows / expected | **anomalies** | retries |
|---|---|---:|---:|---:|---:|---:|
| `no-txn` | autocommit | 15 | 3 | 24 / 6 | **18** | 0 |
| `read-committed` | READ COMMITTED | 22 | 4 | 32 / 6 | **26** | 0 |
| `serializable` | **SERIALIZABLE** | 0 | 0 | **6 / 6** | **0** | 6 |

Every extra row is a memory the agent will later recall as if it were true. In the run above
the unsafe backends ended up believing `ben allergy = penicillin` **and** `ben allergy = sulfa`
simultaneously. Nothing errored. Nothing logged. The memory is just quietly wrong.

The retries in the serializable column are not a cost to hide — they are the mechanism. Six
transactions were aborted with SQLSTATE `40001` and replayed, which is exactly why zero
anomalies were committed.

---

## Why this benchmark did not already exist

The agent-memory field has saturated benchmarks for retrieval (LoCoMo, LongMemEval, DMR,
MemBench) and new ones for temporal validity (STALE, MemConflict, DynamicMem). For
**concurrent writes there is no dataset, no metric, and no leaderboard** — while two 2026
surveys name multi-agent memory the top open frontier and one calls consistency "the most
pressing open challenge."

Meanwhile the defect is live and reproducible in the most-used libraries:

- **Mem0 #6515** — "hash-dedup TOCTOU race in `add()` creates permanent duplicate memories
  under concurrency." Open. *"There is no error, warning, or user-visible signal — the
  duplication is silent and accumulates over time."*
- **Letta #3366** — `ConcurrentUpdateError` deliberately downgraded from ERROR to WARNING.
- **LangGraph Store** — `put` overwrites with no optimistic concurrency control.
- **OpenAI Agents SDK** — documents its own lost-update race in compaction.
- **AWS AgentCore** — shipped `STRICTLY_CONSISTENT` metadata in May 2026 because scoping keys
  *"could only be inferred by the LLM during extraction."*

SKEW turns that from an anecdote into a number.

---

## What it measures

`remember(fact)` is the read-modify-write every memory system performs on ingest:

1. look up the active belief for `(subject, predicate)`
2. same object → reinforce it (`observation_count += 1`)
3. different object → supersede the old row, insert the new one
4. nothing found → insert

Step 1 reads; steps 2–4 write based on what step 1 saw. If a peer commits in that gap, the
decision was made against stale state.

Three anomaly classes are counted from committed state, named after the literature:

| metric | meaning |
|---|---|
| **duplicate rows** | more than one active row for the same `(subject, predicate, object)` — a lost dedup |
| **live contradictions** | two different beliefs simultaneously active for one key — write skew |
| **lost reinforcements** | committed writes not reflected in any `observation_count` — lost update |

---

## Why CockroachDB, specifically

**Serializable is the default.** The correct backend is not exotic — it is the same code in one
transaction. On a database that defaults to a weaker level you must know to ask, and the
`read-committed` column shows what happens when you don't.

**Vectors and rows commit together.** `memories` holds the relational fact *and* its
`VECTOR(1024)` embedding, indexed with C-SPANN. A memory written inside a transaction is
semantically searchable the instant that transaction commits. The `/` dashboard demonstrates
read-your-writes: store a fact, search for it with zero delay, find it. On an eventually
consistent vector store this is the classic miss — the agent asks the user again.

**Two systems cannot be made consistent by trying harder.** The usual stack is Postgres for
facts plus a vector database for embeddings, with no transaction spanning them. Every anomaly
SKEW counts gets worse when the write is split across two stores.

---

## The thing we got wrong first (and it is the interesting part)

The first implementation used the **vector index** for the dedup lookup — `ORDER BY embedding
<=> $1 LIMIT 1` — and serializable scored *16 anomalies*, not zero. Two failures were hiding
in that:

1. **An approximate search is the wrong tool for identity.** Paraphrases of the same fact sat
   further apart than the similarity threshold, so dedup silently became INSERT. The benchmark
   was measuring embedding quality, not concurrency.
2. **Identity must be relational.** Anchoring the lookup on `(run_id, backend, subject,
   predicate)` puts a real predicate in the transaction's read set, so a concurrent insert into
   that group forces a `40001` retry. That is what makes serializable actually serialize.

So: **similarity ranks, it never decides.** Embeddings do semantic recall; the relational key
does identity. Getting this backwards is an easy and quiet way to build a memory layer that
corrupts itself under load.

---

## Running it

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python "psycopg[binary]" certifi fastapi "uvicorn[standard]"

echo 'CRDB_URL="postgresql://USER:PASS@HOST:26257/defaultdb?sslmode=verify-full&sslrootcert=system"' > .env

python run_bench.py --reset --concurrency 24 --repeats 14   # CLI
python app.py                                               # dashboard on :8080
```

`--concurrency` is the number of parallel writers; `--repeats` how many times each fact is
written. Anomalies rise with both.

> **Note on vector indexes:** they are created *with* the table in `db/schema.sql`. Adding one
> to a populated table blocks writes during backfill, and `IMPORT INTO` is unsupported on
> vector-indexed tables.

---

## Architecture

```
 browser ── SSE ──► FastAPI (app.py)
                      │
                      ├─ harness.py     ThreadPoolExecutor, N concurrent writers
                      ├─ backends/      no-txn │ read-committed │ serializable
                      ├─ embed.py       Bedrock Titan v2 (1024-d), local fallback
                      └─ recall.py      C-SPANN similarity + EXPLAIN
                      │
                      ▼
             CockroachDB  ── memories(… VECTOR(1024), VECTOR INDEX)
                          └─ memory_events   append-only audit of every attempt
```

`embed.py` uses Amazon Bedrock Titan Text Embeddings V2 when AWS credentials are present and a
deterministic local encoder otherwise, at the same 1024 dimensions so the schema is identical
either way. Concurrency results do not depend on which is active — by design.

## Tools used

**CockroachDB** — distributed vector indexing (C-SPANN); serializable transactions with `40001`
retry handling; `SELECT … FOR UPDATE`; `AS OF SYSTEM TIME`; `EXPLAIN` surfaced in the UI.
**AWS** — Amazon Bedrock (Titan Text Embeddings V2).

## Licence

Apache-2.0.
