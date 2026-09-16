"""Shared production journal. SQLite remains the zero-setup local backend.

Only application-owned SQL reaches this boundary. Parameters remain bound, never
interpolated. A small compatibility layer preserves the audited evaluator's SQL
without changing its source/version; this is not a general SQLite translator.
"""
import json
import logging
import os
import re
from contextlib import contextmanager
from functools import lru_cache


@lru_cache(maxsize=4)
def pool(url):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
    return ConnectionPool(url, min_size=0, max_size=int(os.getenv("QUALITY_DB_POOL_SIZE", "16")),
                          timeout=15, kwargs={"row_factory": dict_row, "connect_timeout": 10})


def translate(statement, binding=False):
    statement = statement.strip()
    if statement == "BEGIN IMMEDIATE":
        # Serialize infrequent control-plane decisions, not queue consumers.
        return "SELECT pg_advisory_xact_lock(73190511)"
    if statement.startswith("INSERT OR IGNORE INTO "):
        statement = statement.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1).rstrip(";") + " ON CONFLICT DO NOTHING"
    statement = statement.replace(" IS ?", " IS NOT DISTINCT FROM ?")
    statement = statement.replace("SUM(evaluated IS NOT NULL)", "SUM(CASE WHEN evaluated IS NOT NULL THEN 1 ELSE 0 END)")
    statement = statement.replace("COALESCE(json_extract(payload,'$.retired'),0)=0",
                                  "COALESCE(json_extract(payload,'$.retired'),'false') IN ('false','0')")
    if binding:
        statement = statement.replace("%", "%%")
        statement = re.sub(r"'(?:''|[^'])*'|\?", lambda m: "%s" if m[0] == "?" else m[0], statement)
    return statement


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, statement, parameters=()):
        return self.raw.execute(translate(statement, bool(parameters)), tuple(parameters) if parameters else None)


@contextmanager
def connection(url):
    with pool(url).connection() as con:
        yield Connection(con)


def initialize(url, sqlite_schema):
    # All tables have the same columns/types in both backends. Replace only the
    # two SQLite trigger definitions and numeric/sequence types for PostgreSQL.
    start = sqlite_schema.index("CREATE TRIGGER IF NOT EXISTS benchmark_versions_no_update")
    end = sqlite_schema.index("CREATE TABLE IF NOT EXISTS incidents")
    schema = sqlite_schema[:start] + sqlite_schema[end:]
    schema = schema.replace("id INTEGER PRIMARY KEY", "id BIGSERIAL PRIMARY KEY")
    schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)
    with pool(url).connection() as con:
        con.execute("SELECT pg_advisory_xact_lock(73190512)")
        con.execute(schema)
        con.execute("""
            CREATE OR REPLACE FUNCTION json_extract(document TEXT, path TEXT)
            RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
              SELECT document::jsonb #>> string_to_array(substr(path, 3), '.')
            $$;
            CREATE OR REPLACE FUNCTION protect_benchmark_version()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              RAISE EXCEPTION 'Saved benchmark versions are immutable';
            END $$;
            CREATE OR REPLACE TRIGGER benchmark_versions_immutable
              BEFORE UPDATE OR DELETE ON benchmark_versions
              FOR EACH ROW EXECUTE FUNCTION protect_benchmark_version();
            CREATE INDEX IF NOT EXISTS evaluation_ready ON jobs(available, id)
              WHERE kind='evaluate' AND state IN ('pending','running');
            CREATE OR REPLACE FUNCTION notify_quality_job()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.state='pending' THEN
                PERFORM pg_notify('quality_jobs', NEW.kind);
              END IF;
              RETURN NEW;
            END $$;
            CREATE OR REPLACE TRIGGER quality_job_notification
              AFTER INSERT OR UPDATE OF state,available ON jobs
              FOR EACH ROW EXECUTE FUNCTION notify_quality_job();
        """)


def claim(url, kinds, lease_seconds, now, owner):
    if not kinds:
        return None
    with pool(url).connection() as con:
        row = con.execute("""
            WITH candidate AS (
              SELECT id FROM jobs WHERE kind = ANY(%s) AND
                ((state='pending' AND available<=%s) OR (state='running' AND lease_until<%s))
              ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs SET state='running',owner=%s,lease_until=%s,attempts=attempts+1
            FROM candidate WHERE jobs.id=candidate.id RETURNING jobs.*
        """, (list(kinds), now, now, owner, now+lease_seconds)).fetchone()
    return {**row, "payload": json.loads(row["payload"])} if row else None
