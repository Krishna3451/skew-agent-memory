# Devpost submission copy — paste-ready

## Project name (60 char limit)
```
SKEW — agent memory under concurrency
```
`37 chars`

## Elevator pitch (200 char limit)
```
Agent memory is benchmarked on recall, never on staying correct when several agents write at once. SKEW measures that. Same code, three isolation levels, one CockroachDB cluster: 26 anomalies vs zero.
```
`199 chars`

---

## About the project (Markdown)

## Inspiration

Every benchmark for AI agent memory measures recall — can it find the fact later. We went
looking for the one that measures whether memory stays *correct* when several agents write at
the same time, and there isn't one. No dataset, no metric, no leaderboard. Meanwhile two 2026
surveys name multi-agent memory the top open frontier, and one calls consistency "the most
pressing open challenge."

The gap is not academic. It is open, reproducible, and sitting in the libraries people ship on:

- **Mem0 #6515** — a TOCTOU race in `add()` that creates permanent duplicate memories under
  concurrency. Still open. In the reporter's words: *"There is no error, warning, or
  user-visible signal — the duplication is silent and accumulates over time."*
- **Letta #3366** — `ConcurrentUpdateError` deliberately downgraded from ERROR to WARNING.
- **LangGraph Store** — `put` overwrites, with no optimistic concurrency control.
- **AWS AgentCore** — shipped `STRICTLY_CONSISTENT` metadata in May 2026 precisely because
  scoping keys *"could only be inferred by the LLM during extraction."*

So we built the missing yardstick.

## What it does

SKEW fires a concurrent write workload at the same ingest logic implemented three ways, against
one CockroachDB cluster, and counts what breaks.

`remember(fact)` is the read-modify-write every memory system performs on ingest: look up the
active belief for a key, reinforce it if the fact agrees, supersede it if it contradicts, insert
if nothing is there. The read informs the write. If a peer commits in that gap, the decision was
made against stale state.

Three anomaly classes are counted from committed state, named after the database literature:

- **duplicate rows** — more than one active row for the same fact (a lost dedup)
- **live contradictions** — two different beliefs simultaneously active for one key (write skew)
- **lost reinforcements** — committed writes reflected in no `observation_count` (lost update)

84 concurrent writes describing 6 distinct facts. Correct behaviour is 6 active rows.

| backend | isolation | duplicates | contradictions | rows / expected | anomalies | retries |
|---|---|---:|---:|---:|---:|---:|
| no-txn | autocommit | 15 | 3 | 24 / 6 | **18** | 0 |
| read-committed | READ COMMITTED | 22 | 4 | 32 / 6 | **26** | 0 |
| serializable | **SERIALIZABLE** | 0 | 0 | **6 / 6** | **0** | 6 |

The unsafe backends ended up believing `ben allergy = penicillin` **and** `ben allergy = sulfa`
at the same time. Nothing errored. An agent reading that memory cannot tell which is true.

## How we built it

Identical code, identical database, identical workload. The only variable is transaction
discipline — no strawman baseline, no simulated competitor.

**CockroachDB** holds the relational fact and its `VECTOR(1024)` embedding in one table, indexed
with C-SPANN. The correct backend runs the whole read-modify-write inside a single serializable
transaction with `SELECT … FOR UPDATE` and a bounded `40001` retry loop. Retry counts are shown
in the UI — the cost of correctness is displayed, not hidden.

Because vectors and rows commit together, a memory written inside a transaction is semantically
searchable the instant that transaction commits. The dashboard demonstrates read-your-writes:
store a fact, search with zero delay, find it. On an eventually consistent vector store this is
the classic miss — the agent asks the user again.

**Amazon Bedrock** (Titan Text Embeddings V2, 1024-d) generates embeddings, behind an interface
with a deterministic local encoder at identical dimensions, so the schema never changes.
Concurrency results do not depend on which is active — by design.

The dashboard is FastAPI streaming SSE to a dependency-free front end.

## Challenges we ran into

**The first version scored 16 anomalies on serializable, not zero.** We had used the vector index
for the dedup lookup — `ORDER BY embedding <=> $1 LIMIT 1`. Two failures were hiding in that.

An approximate search is the wrong tool for identity: paraphrases of the same fact sat further
apart than the similarity threshold, so dedup silently became INSERT, and the benchmark was
measuring embedding quality rather than concurrency. Worse, an ANN scan does not establish a
serializable read span over the key group — two transactions can both search, both see nothing,
and both insert, because neither read the span the other wrote into. Read-refresh has nothing to
conflict on.

Anchoring the lookup on the relational key `(run_id, backend, subject, predicate)` puts a real
predicate in the read set, so a concurrent insert forces a retry. **Similarity ranks; it never
decides identity.** Getting that backwards is a quiet way to build a memory layer that corrupts
itself under load.

Two smaller ones worth recording: a vector index must be created *with* the table, because
backfill on a populated table blocks writes; and vector-index prefix columns are equality-only,
so a range filter is applied after the ANN search and can silently return fewer rows than the
`LIMIT`.

## What we learned

Agent memory is a concurrency problem wearing an AI costume. The interesting failures are not
retrieval failures — they are lost updates and write skew, which the database field named and
solved decades ago and the agent field has not yet imported. Serializable-by-default meant the
correct implementation was the *obvious* one; the `read-committed` column shows what happens on
a database where you have to know to ask.

## What's next

Adapters so third-party memory systems can be scored on the same workload, turning SKEW into a
comparative leaderboard rather than a demonstration. A multi-region survival test — this run is
single-region because CockroachDB Basic is. And extending the anomaly set to the cross-transaction
cases recent work names but does not measure: source-scope laundering across retry boundaries,
and stale-write-after-retry.

---

## Built with (tags)
```
cockroachdb, distributed-sql, serializable-isolation, vector-search, c-spann,
amazon-bedrock, amazon-titan-embeddings, python, fastapi, uvicorn, psycopg3,
server-sent-events, docker, ai-agents, agent-memory, benchmarking, concurrency
```

## Try it out links
- Live demo: `<DEMO_URL>`
- GitHub: https://github.com/Krishna3451/skew-agent-memory

---

# Additional info (judges only)

**Which CockroachDB tools:** Distributed Vector Indexing · Cloud Managed MCP Server *(select
only what is genuinely used — see the honesty note below)*

**Which AWS services:** Amazon Bedrock

**How the components were meaningfully integrated:**
```
CockroachDB is the subject of the project, not its storage. The benchmark exists to measure a
property of the database: whether serializable isolation prevents memory-corrupting anomalies
that weaker disciplines permit. The write path is a locking read plus up to three dependent
writes inside one transaction, with SQLSTATE 40001 retry handling and retry counts surfaced in
the UI. Distributed vector indexing (C-SPANN) is created with the table and used for semantic
recall over the same rows the transactions write, which is what makes read-your-writes
demonstrable: a vector inserted in a transaction is searchable the moment it commits, with no
second store to synchronise. EXPLAIN output is shown in the dashboard to prove the index is
used rather than asserted.

Amazon Bedrock (Titan Text Embeddings V2, 1024 dimensions) generates the embeddings written
alongside each memory row, behind an interface that falls back to a deterministic local encoder
of identical dimensionality so the schema is unchanged either way.
```

**Start date:** `08-18-26`

**Pre-existing code:** 
```
None. All source in the repository was written during the submission period. Standard
third-party libraries are used unmodified via requirements.txt: psycopg3 (CockroachDB driver),
FastAPI and uvicorn (web layer), boto3 (Bedrock client), certifi (TLS roots). An AI coding
assistant (Claude) was used during development. No pre-existing project, template, or codebase
was incorporated.
```

**Which AI tools did you leverage:**
```
Claude (Anthropic) via Claude Code, for research, architecture and implementation.
```

**Feedback on CockroachDB AI tools:**
```
Two things cost us real time and would be worth surfacing in the vector index docs.

First, that an approximate vector search does not establish a serializable read span. We used
ORDER BY embedding <=> $1 LIMIT 1 as a dedup lookup inside a serializable transaction and it
did not conflict with concurrent inserts into the same logical key group, because the ANN scan
never read the span the other transaction wrote into. This is correct behaviour but very
surprising, and a note next to the vector index documentation would help people avoid building
a memory layer that silently loses writes.

Second, the interaction between vector indexes and prefix columns being equality-only is
documented, but the consequence is not spelled out: a non-equality predicate is applied after
the ANN search, so a filtered query can silently return fewer rows than its LIMIT. That reads
as a bug in application code rather than expected index behaviour.

Positive: serializable-by-default meant the correct implementation was also the obvious one,
and having vectors and relational rows commit in a single transaction removed an entire class
of dual-write synchronisation bugs we would otherwise have had to design around.
```

---

## ⚠ Honesty note before you submit

Only tick a CockroachDB tool if it is genuinely exercised in the code. As of this writing
**Distributed Vector Indexing** is real and load-bearing. If the MCP server is not actually
wired in, do not tick it — the form asks for meaningful integration and the repo is public.
The same applies to Bedrock: if it never ran with real credentials, the honest answer is that
the integration is code-complete but unexercised, and the local encoder was used. Judges can
read `embed.py`.
