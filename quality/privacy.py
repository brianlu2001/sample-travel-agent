"""Local redaction before any feedback payload is persisted or transmitted.

The NLP model is installed explicitly during setup, never downloaded at runtime.
Destination cities are allowed business context; personal street addresses and
identifiers are not. No reverse lookup of redacted values is persisted.
"""
import json
import re
import threading
from functools import lru_cache

LOCK = threading.RLock()
RULES = [
    ("SECRET", re.compile(r"\bak-[A-Za-z0-9_-]{20,}\b")),
    ("SECRET", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\w)")),
    ("ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PAYMENT", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ADDRESS", re.compile(r"\b\d{1,6}[ \t]+(?:[\w-]+[ \t]+){1,5}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd)\b(?:[ \t]+(?:apt|unit|suite)[ .#]*[\w-]+)?", re.I)),
    ("ID", re.compile(r"(?i)\b(?:passport|booking reference|confirmation code|reservation number)(?:\s+(?:number|no\.?))?\s*(?:is\s+|[:#]\s*)?(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,}\b")),
    ("ID", re.compile(r"\b[A-Z]\d{8}\b", re.I)),
    ("PERSON", re.compile(r"(?i)\b(?:my name is|i am named|i'm named)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")),
]

# Domain terms may be misclassified as names by a small general NLP model.
# Explicit self-identification is removed above, before this exception applies.
ALLOWED_BUSINESS_TERMS = {"austin", "paris", "new york", "miami", "chicago", "london", "convert"}


@lru_cache(maxsize=1)
def analyzer():
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    import spacy.util
    if not spacy.util.is_package("en_core_web_sm"):
        raise RuntimeError("PII model missing; run python -m spacy download en_core_web_sm")
    engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }).create_engine()
    return AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])


def redact_text(text: str) -> str:
    for entity, pattern in RULES:
        text = pattern.sub(f"[{entity}]", text)
    with LOCK:
        hits = analyzer().analyze(text=text, language="en", entities=["PERSON", "US_SSN", "CREDIT_CARD", "US_PASSPORT", "IP_ADDRESS"], score_threshold=0.65)
    for hit in sorted(hits, key=lambda h: h.start, reverse=True):
        if hit.entity_type == "PERSON" and text[hit.start:hit.end].casefold() in ALLOWED_BUSINESS_TERMS:
            continue
        text = text[:hit.start] + f"[{hit.entity_type}]" + text[hit.end:]
    return text


def sanitize(value):
    if isinstance(value, str):
        # Redact nested JSON structurally so keys, IDs, types, and numbers survive.
        if value.startswith(("{", "[")):
            try:
                return json.dumps(sanitize(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return redact_text(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if re.search(r"(?i)(api.?key|authorization|password|secret|access.?token)", str(key)):
                out[str(key)] = "[SECRET]"
            else:
                out[str(key)] = sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return sanitize(value.model_dump(mode="json"))
    return redact_text(str(value))


def safe_payload(value):
    try:
        return sanitize(value)
    except Exception:
        return {"privacy_error": "redaction_unavailable", "content": "[WITHHELD]"}
