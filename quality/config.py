"""Explicit, environment-configurable settings; no credentials in telemetry."""
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
STATE = Path(os.getenv("QUALITY_STATE_DIR", str(ROOT / ".quality"))).resolve()
DB = STATE / "quality.sqlite3"
DATABASE_URL = os.getenv("QUALITY_DATABASE_URL", "")
PHOENIX = os.getenv("PHOENIX_ENDPOINT", os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006")).rstrip("/")
JUDGE_MODEL = os.getenv("QUALITY_JUDGE_MODEL", "claude-sonnet-4-6")
REPAIR_MODEL = os.getenv("QUALITY_REPAIR_MODEL", JUDGE_MODEL)
PROJECT = os.getenv("PHOENIX_PROJECT", "travel-agent")
ARIZE_API_KEY = os.getenv("ARIZE_API_KEY", "")
ARIZE_SPACE_ID = os.getenv("ARIZE_SPACE_ID", "")
ARIZE_OTLP_ENDPOINT = os.getenv("ARIZE_OTLP_ENDPOINT", "https://otlp.arize.com/v1").rstrip("/")
METRICS = ("correctness", "groundedness", "topic_relevance", "task_completion")
# Keep the audited evaluator and historical evidence intact. Completion is a
# diagnostic only; active monitoring and new checkpoint decisions use these three.
PRIMARY_METRICS = ("correctness", "groundedness", "topic_relevance")
WINDOW = int(os.getenv("QUALITY_WINDOW", "20"))
MIN_SAMPLES = int(os.getenv("QUALITY_MIN_SAMPLES", "10"))
THRESHOLD = float(os.getenv("QUALITY_THRESHOLD", "0.85"))
PERSISTENCE = int(os.getenv("QUALITY_BREACH_NEW_SAMPLES", "0"))
RECOVERY = float(os.getenv("QUALITY_RECOVERY_THRESHOLD", "0.95"))
MAX_ATTEMPTS = 3


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(paths) -> str:
    return fingerprint({str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in paths})


def agent_version() -> str:
    return file_hash([ROOT / "agent" / p for p in ("loop.py", "prompt.py", "tools.py", "config.py")])


def fixture_version() -> str:
    return file_hash(sorted((ROOT / "data").glob("*.json")))
