-- SKEW: agent-memory concurrency benchmark
-- Vector indexes are created WITH the table (never added to a populated table:
-- backfill on a non-empty table blocks writes).

DROP TABLE IF EXISTS memory_events;
DROP TABLE IF EXISTS memories;

-- The contested resource. One row per believed fact, per benchmark run, per backend.
CREATE TABLE memories (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    run_id            UUID        NOT NULL,
    backend           STRING      NOT NULL,
    subject           STRING      NOT NULL,
    predicate         STRING      NOT NULL,
    object            STRING      NOT NULL,
    content           STRING      NOT NULL,
    embedding         VECTOR(1024),
    confidence        FLOAT       NOT NULL DEFAULT 0.5,
    observation_count INT         NOT NULL DEFAULT 1,
    status            STRING      NOT NULL DEFAULT 'active',
    superseded_by     UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_memories PRIMARY KEY (id),
    INDEX idx_run (run_id, backend, subject, predicate),
    -- Prefix columns are EQUALITY-ONLY on vector indexes; run_id and backend are
    -- always filtered by equality, so acceleration holds.
    VECTOR INDEX vec_mem (run_id, backend, embedding)
);

-- Append-only audit spine. Every attempted write lands here regardless of outcome,
-- so anomalies can be attributed after the fact.
CREATE TABLE memory_events (
    id         UUID        NOT NULL DEFAULT gen_random_uuid(),
    run_id     UUID        NOT NULL,
    backend    STRING      NOT NULL,
    worker     INT         NOT NULL,
    op         STRING      NOT NULL,   -- insert | reinforce | supersede | conflict_abort
    memory_id  UUID,
    subject    STRING,
    predicate  STRING,
    object     STRING,
    retries    INT         NOT NULL DEFAULT 0,
    latency_ms FLOAT,
    txn_ok     BOOL        NOT NULL DEFAULT true,
    detail     STRING,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_events PRIMARY KEY (id),
    INDEX idx_ev_run (run_id, backend, at)
);
