"""Conservative structural checks on the final answer, independent of tool drafts.

These checks measure day coverage, not whether the activities are useful or safe.
Unsupported/ambiguous formats defer to semantic evaluation instead of guessing.
"""
import re

WORDS = {word: i for i, word in enumerate(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split(), 1)}
NUMBER = r"(?:\d{1,2}|" + "|".join(WORDS) + r")"
DURATION = re.compile(r"\b(" + NUMBER + r")[\s-]+days?\b", re.I)
HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?(?:[-*+][ \t]+)?\*{0,2}Day[ \t]+(" + NUMBER + r")\b(.*)$", re.I | re.M)
MARKERS = re.compile(r"\[(?:PERSON|EMAIL|PHONE|ID|PAYMENT|ADDRESS|SECRET|US_SSN|CREDIT_CARD|US_PASSPORT|IP_ADDRESS)\]")


def number(text):
    return int(text) if text.isdigit() else WORDS[text.casefold()]


def day_coverage(conversation, final_response):
    """Only explicit user durations and numbered day sections are machine-checkable."""
    requested = None
    for message in conversation if isinstance(conversation, list) else []:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        durations = {number(m.group(1)) for m in DURATION.finditer(message["content"])}
        if durations:
            requested = next(iter(durations)) if len(durations) == 1 else None
    result = {"name": "final_answer_day_coverage", "requested_days": requested,
              "observed_day_numbers": [], "label": "unknown",
              "scope": "Numbered final-answer sections only; not activity quality or tool output."}
    if not isinstance(final_response, str):
        return result
    matches = list(HEADING.finditer(final_response))
    result["observed_day_numbers"] = [number(m.group(1)) for m in matches]
    if requested is None or not 1 <= requested <= 31 or not matches:
        return result
    # Ranges, combined sections and empty headings need semantic inspection.
    if any(re.match(r"\s*(?:[-–—/&]|and\b)\s*(?:Day\s+)?" + NUMBER + r"\b", m.group(2), re.I) for m in matches):
        return result
    for i, match in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(final_response)
        body = final_response[match.end():end]
        inline = re.sub(r"^[*\s:–—-]+", "", match.group(2))
        if len(re.findall(r"[A-Za-z]+", body + " " + inline)) < 3:
            return result
    observed = result["observed_day_numbers"]
    result["label"] = "pass" if sorted(observed) == list(range(1, requested+1)) else "fail"
    return result


def visible_conversation(messages):
    """Only user-visible dialogue is intent evidence; tool payloads are not users."""
    visible = []
    for message in messages if isinstance(messages, list) else []:
        if message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(block.get("text", "") for block in content
                                if isinstance(block, dict) and block.get("type") == "text")
        if isinstance(content, str) and content.strip():
            visible.append({"role": message["role"], "content": content})
    return visible


def evaluation_input(event, evidence):
    """Draft itineraries are proposals, never a factual authority for final answers."""
    output = event["output"]
    conversation = visible_conversation(event["input"])
    checks = [day_coverage(conversation, output)]
    execution = {"conversation": conversation, "final_response": output,
                 "answer_checks": checks,
                 "privacy_processing": {
                     "content_is_sanitized": True,
                     "final_response_mask_count": len(MARKERS.findall(output)) if isinstance(output, str) else 0,
                     "meaning": "Bracketed privacy markers were inserted by the feedback pipeline; they are not blanks in the user-visible answer. Original masked text is unavailable; never reconstruct it.",
                 }}
    references = [{"tool": e["tool"], "arguments": e["arguments"],
                   "independent_reference": e["independent_reference"]}
                  for e in evidence if e["tool"] != "create_itinerary"]
    return execution, references
