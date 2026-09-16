"""Small development suite and actual incident reproductions, never held-out cases."""
from collections import defaultdict

from quality.answer_checks import visible_conversation
from quality.config import fingerprint
from quality.evaluation import scenarios


def select(evidence, limit=15):
    selected, seen = [], set()
    for item in evidence:
        messages = [m["content"] for m in visible_conversation(item["input"]) if m["role"] == "user"]
        digest = fingerprint(messages)
        if not messages or digest in seen:
            continue
        seen.add(digest)
        selected.append({"id": "incident-" + digest[:16], "category": "incident_reproduction",
                         "split": "development", "messages": messages,
                         "reference": {"origin_run_id": item["run_id"], "behavior": "Satisfy the current request using the independent travel data contract."}})
        if len(selected) == 3:
            break
    groups = defaultdict(list)
    for example in scenarios():
        if example["split"] == "development":
            groups[example["category"]].append(example)
    while len(selected) < limit and any(groups.values()):
        for group in groups.values():
            if group and len(selected) < limit:
                example = group.pop(0)
                digest = fingerprint(example["messages"])
                if digest not in seen:
                    selected.append(example)
                    seen.add(digest)
    return selected
