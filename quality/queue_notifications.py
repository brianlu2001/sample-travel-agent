"""PostgreSQL push wake-ups; durable jobs remain the source of truth."""
from quality import store


class Wakeup:
    def __init__(self):
        self.connection = None
        if store.DATABASE_URL:
            import psycopg
            self.connection = psycopg.connect(store.DATABASE_URL, autocommit=True)
            # Subscribe before the first claim so a commit cannot race registration.
            self.connection.execute('LISTEN quality_jobs')

    def wait(self, stop):
        if self.connection is None:
            stop.wait(.3)  # Local SQLite transport only; never recomputes metrics.
        elif not stop.is_set():
            # Timeout recovers expired leases/retries/missed notifications. NOTIFY
            # carries only a job kind, not prompts, responses or credentials.
            for _ in self.connection.notifies(timeout=5, stop_after=1):
                break

    def close(self):
        if self.connection is not None:
            self.connection.close()
