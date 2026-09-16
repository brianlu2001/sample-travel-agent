"""Constrained candidate execution in disposable, time-limited subprocesses.

Only four pure tool-function replacements and a prompt string are accepted.
Candidate functions have an AST allowlist and restricted builtins: no imports,
filesystem, sockets, reflection, decorators or arbitrary command execution.
This local boundary is not a substitute for a production container/VM sandbox.
"""
import ast
import builtins
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from quality.config import ROOT, fingerprint

FUNCTIONS = {"search_flights", "search_hotels", "get_weather", "create_itinerary"}
BUILTINS = {name: getattr(builtins, name) for name in (
    "int", "str", "float", "bool", "range", "sum", "ord", "round", "next", "len", "set", "list", "dict", "tuple",
    "sorted", "min", "max", "enumerate", "isinstance", "all", "any", "zip", "ValueError", "TypeError", "Exception")}
METHODS = {"lower", "casefold", "strip", "get", "append", "items", "keys", "values", "isoformat", "fromisoformat"}
ALLOWED = {"Module", "FunctionDef", "arguments", "arg", "Return", "Assign", "AnnAssign", "Expr", "If", "For", "Break", "Continue", "Pass",
           "ListComp", "DictComp", "SetComp", "GeneratorExp", "comprehension", "BinOp", "BoolOp", "UnaryOp", "Compare",
           "Subscript", "Slice", "Load", "Store", "Call", "Attribute", "Name", "Constant", "List", "Dict", "Set", "Tuple",
           "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "And", "Or", "Not", "USub", "UAdd", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE",
           "In", "NotIn", "Is", "IsNot", "IfExp", "JoinedStr", "FormattedValue", "keyword", "Raise", "Try", "ExceptHandler"}


def validate_candidate(candidate):
    if set(candidate) - {"prompt", "functions", "summary", "rationale"}:
        raise ValueError("Unexpected candidate fields")
    if not isinstance(candidate.get("prompt"), str) or len(candidate["prompt"]) > 15000:
        raise ValueError("Invalid candidate prompt")
    if not isinstance(candidate.get("functions"), dict) or not set(candidate["functions"]).issubset(FUNCTIONS):
        raise ValueError("Only existing pure travel tools may be changed")
    for name, source in candidate["functions"].items():
        if not isinstance(source, str) or len(source) > 16000:
            raise ValueError("Invalid candidate function")
        tree = ast.parse(source)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != name:
            raise ValueError("Exactly one matching function definition is required")
        fn = tree.body[0]
        if fn.decorator_list or any(not isinstance(x, ast.Constant) for x in fn.args.defaults):
            raise ValueError("Decorators and executable defaults are forbidden")
        for node in ast.walk(tree):
            if type(node).__name__ not in ALLOWED:
                raise ValueError("Forbidden syntax: " + type(node).__name__)
            if isinstance(node, ast.Name) and "__" in node.id:
                raise ValueError("Reflection is forbidden")
            if isinstance(node, ast.Attribute) and node.attr not in METHODS:
                raise ValueError("Forbidden attribute: " + node.attr)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id not in set(BUILTINS) | FUNCTIONS:
                    raise ValueError("Forbidden call: " + node.func.id)
                if not isinstance(node.func, (ast.Name, ast.Attribute)):
                    raise ValueError("Dynamic calls are forbidden")
    return candidate


def load_functions(candidate):
    validate_candidate(candidate)
    from agent import tools
    namespace = {"__builtins__": BUILTINS, "FLIGHTS": tools.FLIGHTS, "HOTELS": tools.HOTELS, "WEATHER": tools.WEATHER, "date": date}
    functions = dict(tools.TOOL_FUNCTIONS)
    for name, source in candidate["functions"].items():
        exec(compile(ast.parse(source), "<candidate-tool>", "exec"), namespace)
        functions[name] = namespace[name]
    return functions


def run_candidate_turn(messages, candidate_path, benchmark_id, scenario_id, conversation_id):
    payload = {"messages": messages, "candidate_path": str(candidate_path), "benchmark_id": benchmark_id,
               "scenario_id": scenario_id, "conversation_id": conversation_id}
    process = subprocess.run([sys.executable, "-X", "utf8", "-m", "quality.sandbox"], input=json.dumps(payload),
                             text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=180,
                             env={**os.environ, "PYTHONUTF8": "1"})
    if process.returncode:
        raise RuntimeError("Candidate execution failed; no raw process output retained")
    result = json.loads(process.stdout)
    return result["reply"], result["messages"], result["event"]


def main():
    payload = json.load(sys.stdin)
    candidate = json.loads(Path(payload["candidate_path"]).read_text(encoding="utf-8"))
    functions = load_functions(candidate)
    from agent import loop
    loop.SYSTEM_PROMPT = candidate["prompt"]

    def execute(name, args):
        try:
            return functions[name](**args)
        except Exception as error:
            return {"error": str(error)}
    loop.execute_tool = execute
    from quality.runtime import run_observed
    reply, messages, event = run_observed(payload["messages"], source="benchmark", benchmark_id=payload["benchmark_id"],
        scenario_id=payload["scenario_id"], conversation_id=payload["conversation_id"], version_override=fingerprint(candidate))
    # Anthropic blocks in history are converted to JSON for the next turn.
    def encode(obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        raise TypeError(type(obj).__name__)
    sys.stdout.write(json.dumps({"reply": reply, "messages": messages, "event": event}, default=encode))


if __name__ == "__main__":
    main()
