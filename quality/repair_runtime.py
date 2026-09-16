"""One Claude Agent SDK session per repair; Phoenix tools run in the worker.

The model has an isolated scratch workspace and native Skill/Read tools. It
submits a candidate through MCP; the existing controller owns tests and GitHub.
"""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions, HookMatcher, ResultMessage, StreamEvent, SystemMessage,
    create_sdk_mcp_server, query, tool,
)

from quality.config import REPAIR_MODEL
from quality.privacy import safe_payload
from quality.repair_skills import MAX_ROUNDS, MAX_TOOL_CALLS, PURPOSES, TOOLS, catalog, read_document
from quality.sandbox import validate_candidate
from quality.tracing import error_status, ids, set_io, setup

PATCH_SCHEMA = {"type": "object", "properties": {
    "prompt": {"type": "string"}, "summary": {"type": "string"}, "rationale": {"type": "string"},
    "functions": {"type": "object", "additionalProperties": {"type": "string"}}},
    "required": ["prompt", "summary", "rationale", "functions"], "additionalProperties": False}


def prepare_workspace(directory):
    root = Path(directory)
    # A repository boundary prevents discovery of parent project instructions.
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n", encoding="utf-8")
    documents = {}
    for skill in catalog():
        for document in skill["documents"]:
            content = read_document(skill["name"], document)["content"]
            path = root / ".claude" / "skills" / skill["name"] / document
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            documents[path.resolve()] = (skill["name"], document)
    return documents


def runtime_environment(directory):
    # SDK merges options.env with its parent environment. Explicitly blank
    # everything except the provider key and OS runtime variables.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
               "LANG", "LC_ALL", "ANTHROPIC_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value if key.upper() in allowed else "" for key, value in os.environ.items()}
    home = Path(directory) / "home"
    home.mkdir()
    env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
               DISABLE_ERROR_REPORTING="1", CLAUDE_CODE_ENABLE_TELEMETRY="0")
    return env


class Session:
    def __init__(self, investigation, directory, original_prompt):
        self.investigation = investigation
        self.directory = Path(directory)
        self.documents = prepare_workspace(directory)
        self.original_prompt = original_prompt
        self.candidate = None
        self.finding = None
        self.tool_count = 0
        self.native_spans = {}
        self.metadata = {"runtime": "claude-agent-sdk", "model": REPAIR_MODEL}

    def native_document(self, name, arguments):
        if name == "Skill" and arguments.get("skill") in PURPOSES:
            return arguments["skill"], "SKILL.md"
        if name == "Read" and not arguments.get("offset") and not arguments.get("limit"):
            path = Path(arguments.get("file_path", ""))
            if not path.is_absolute():
                path = self.directory / path
            if path.resolve() in self.documents:
                return self.documents[path.resolve()]
        return None

    async def before_tool(self, event, tool_id, context):
        self.tool_count += 1
        name, arguments = event["tool_name"], event["tool_input"]
        approved = name in {"mcp__phoenix__" + t["name"] for t in TOOLS} | {"mcp__phoenix__propose_patch"}
        document = self.native_document(name, arguments)
        if self.tool_count > MAX_TOOL_CALLS or self.candidate is not None or self.finding is not None:
            approved = False
            document = None
        if not approved and document is None:
            self.investigation.calls.append({"tool": name, "arguments": safe_payload(arguments), "status": "denied"})
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Only packaged skills, scoped Phoenix evidence, and one terminal proposal are available within the tool budget."}}
        if document:
            # Verify the pinned source and the staged file before a native read.
            source = read_document(*document)
            path = self.directory / ".claude" / "skills" / document[0] / document[1]
            if path.read_text(encoding="utf-8") != source["content"]:
                raise ValueError("Runtime skill bundle changed")
            span = setup().start_span("repair.skill_load" if name == "Skill" else "repair.skill_reference", openinference_span_kind="tool")
            span.set_attribute("tool.name", name)
            set_io(span, {"skill": document[0], "document": document[1]})
            self.native_spans[tool_id] = (span, document)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}

    async def after_tool(self, event, tool_id, context):
        pending = self.native_spans.pop(tool_id, None)
        if pending:
            span, document = pending
            failed = event["hook_event_name"] == "PostToolUseFailure"
            try:
                if failed:
                    error_status(span, RuntimeError())
                else:
                    usage = self.investigation.document_loaded(*document)
                    set_io(span, {"skill": document[0], "document": document[1]}, usage)
                self.investigation.calls.append({"tool": event["tool_name"], "arguments": {"skill": document[0], "document": document[1]},
                                                 "status": "error" if failed else "ok", **ids(span)})
            finally:
                span.end()
        return {}

    async def call(self, name, arguments):
        if name not in ("propose_patch", "report_evaluator_issue"):
            return await self._call(name, arguments)
        with setup().start_as_current_span("repair." + name, openinference_span_kind="tool",
                                          record_exception=False, set_status_on_exception=False) as span:
            span.set_attribute("tool.name", name)
            result = await self._call(name, arguments)
            set_io(span, arguments, result)
            self.investigation.calls.append({"tool": name, "status": "error" if result["isError"] else "ok", **ids(span)})
            if result["isError"]:
                error_status(span, ValueError())
            return result

    async def _call(self, name, arguments):
        if self.candidate is not None or self.finding is not None:
            result = {"error": "Investigation already finished"}
        elif name == "propose_patch":
            result = self.investigation.ready(prompt_changed=arguments.get("prompt") != self.original_prompt)
            if result:
                result = {"error": "InvestigationIncomplete", **result}
            else:
                try:
                    validate_candidate(arguments)
                    self.candidate = arguments
                    result = {"accepted": True, "instruction": "Stop. The controller now owns validation and PR publication."}
                except ValueError:
                    result = {"error": "Candidate rejected by the existing patch constraints"}
        elif name == "report_evaluator_issue":
            try:
                self.finding = self.investigation.evaluator_issue(arguments)
                result = {"recorded": True, "instruction": "Stop. Human evidence review is required; no patch was accepted."}
            except ValueError as error:
                result = {"error": str(error)}
        else:
            result = await asyncio.to_thread(self.investigation.call, name, arguments)
        return {"content": [{"type": "text", "text": json.dumps(safe_payload(result))}], "isError": "error" in result}

    def mcp(self):
        tools = []
        for definition in TOOLS + [{"name": "propose_patch", "description": "Submit a complete bounded candidate after investigating real evidence.", "input_schema": PATCH_SCHEMA}]:
            async def handler(arguments, name=definition["name"]):
                return await self.call(name, arguments)
            tools.append(tool(definition["name"], definition["description"], definition["input_schema"])(handler))
        return create_sdk_mcp_server(name="phoenix", version="1.0.0", tools=tools)

    def options(self, system):
        return ClaudeAgentOptions(
            model=REPAIR_MODEL, system_prompt=system, cwd=str(self.directory),
            tools=["Skill", "Read"], skills=list(PURPOSES), setting_sources=["project"],
            mcp_servers={"phoenix": self.mcp()}, strict_mcp_config=True,
            permission_mode="dontAsk", hooks={
                "PreToolUse": [HookMatcher(hooks=[self.before_tool])],
                "PostToolUse": [HookMatcher(hooks=[self.after_tool])],
                "PostToolUseFailure": [HookMatcher(hooks=[self.after_tool])]},
            max_turns=MAX_ROUNDS, max_budget_usd=3.0, include_partial_messages=True,
            env=runtime_environment(self.directory), stderr=lambda _: None,
            extra_args={"no-session-persistence": None},
        )


async def run_session(investigation, system, payload, original_prompt):
    with tempfile.TemporaryDirectory(prefix="quality-repair-") as directory:
        session = Session(investigation, directory, original_prompt)
        llm_span = None
        usage, output = {}, []
        result = None
        try:
            async with asyncio.timeout(600):
                async for message in query(prompt=json.dumps(safe_payload(payload)), options=session.options(system)):
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        session.metadata["discovered_skills"] = message.data.get("skills", [])
                    elif isinstance(message, StreamEvent):
                        event = message.event
                        if event["type"] == "message_start":
                            llm_span = setup().start_span("repair.claude", openinference_span_kind="llm", start_time=time.time_ns())
                            llm_span.set_attribute("llm.model_name", event["message"].get("model", REPAIR_MODEL))
                            usage, output = dict(event["message"].get("usage", {})), []
                        elif event["type"] == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                output.append({"tool": block.get("name")})
                        elif event["type"] == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                            output.append(event["delta"].get("text", ""))
                        elif event["type"] == "message_delta":
                            usage.update(event.get("usage", {}))
                        elif event["type"] == "message_stop" and llm_span:
                            if "input_tokens" in usage:
                                llm_span.set_attribute("llm.token_count.prompt", sum(usage.get(k, 0) for k in
                                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
                            if "output_tokens" in usage:
                                llm_span.set_attribute("llm.token_count.completion", usage["output_tokens"])
                            for key, target in (("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_write")):
                                if key in usage:
                                    llm_span.set_attribute("llm.token_count.prompt_details." + target, usage[key])
                            set_io(llm_span, {"evidence_run_ids": sorted(investigation.run_ids)}, output)
                            llm_span.end()
                            llm_span = None
                    elif isinstance(message, ResultMessage):
                        result = message
                        session.metadata.update(session_id=message.session_id, turns=message.num_turns,
                                                estimated_cost_usd=message.total_cost_usd, usage=message.usage,
                                                terminal_reason=message.terminal_reason, result_subtype=message.subtype)
        finally:
            if llm_span:
                error_status(llm_span, RuntimeError())
                llm_span.end()
            for span, _ in session.native_spans.values():
                error_status(span, RuntimeError())
                span.end()
        if result is None or result.is_error or result.subtype != "success" or result.terminal_reason not in (None, "completed"):
            raise RuntimeError("Repair SDK session did not complete: " + str(result.subtype if result else "no_result"))
        if session.candidate is None and session.finding is None:
            raise RuntimeError("Repair SDK session completed without an evidence-backed outcome")
        return session.candidate, session.finding, session.metadata
