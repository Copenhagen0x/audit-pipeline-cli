"""Tool-using agent loop — gives Claude file-access tools.

This is the upgrade from speculation-based recon to grounded bug-hunting.
The agent receives `read_file`, `grep`, `find_function` tools and iterates
until it has enough context to render a verdict with concrete line
citations.

This is what enabled qedbot to find Percolator #60 — iterative code
exploration with the LLM steering its own searches.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ToolUsingResult:
    text: str
    input_tokens: int
    output_tokens: int
    n_turns: int
    tool_calls: list[dict[str, Any]]
    stop_reason: str


# ---------------------------------------------------------------------------
# Tool implementations (run on the local filesystem)
# ---------------------------------------------------------------------------


def _normalize_path(workspace: Path, path: str) -> Path | None:
    """Resolve a tool-supplied path, refusing escapes outside workspace."""
    try:
        p = (workspace / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    except OSError:
        return None
    try:
        ws = workspace.resolve()
    except OSError:
        return None
    # Allow paths inside the workspace OR inside /root/audit_runs (for the
    # full Cargo workspace which lives at workspace/target/...)
    p_str = str(p)
    if not (p_str.startswith(str(ws)) or p_str.startswith("/root/audit_runs")):
        return None
    return p


def tool_read_file(workspace: Path, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a file (or range of lines) and return with line numbers."""
    p = _normalize_path(workspace, path)
    if p is None or not p.exists() or not p.is_file():
        return f"ERROR: file not found or outside workspace: {path}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR reading {path}: {e}"
    lines = text.splitlines()
    s = max(1, start_line)
    e = min(len(lines), end_line) if end_line else min(len(lines), s + 250)
    if s > len(lines):
        return f"ERROR: start_line {start_line} > file has {len(lines)} lines"
    width = len(str(e))
    out = [f"--- {p.name} (lines {s}-{e} of {len(lines)}) ---"]
    out += [f"{i:>{width}}: {lines[i - 1]}" for i in range(s, e + 1)]
    return "\n".join(out)


def tool_grep(workspace: Path, pattern: str, path: str = ".", max_matches: int = 50) -> str:
    """Find `pattern` (regex) in files under `path`. Returns line:content matches."""
    p = _normalize_path(workspace, path)
    if p is None or not p.exists():
        return f"ERROR: path not found: {path}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex `{pattern}`: {e}"
    files = []
    if p.is_file():
        files = [p]
    else:
        for ext in (".rs", ".md"):
            files.extend(p.rglob(f"*{ext}"))
    matches: list[str] = []
    for f in files:
        try:
            for ln, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if rx.search(line):
                    rel = f.name if not f.is_relative_to(workspace) else str(f.relative_to(workspace))
                    matches.append(f"{rel}:{ln}: {line}")
                    if len(matches) >= max_matches:
                        break
        except OSError:
            continue
        if len(matches) >= max_matches:
            break
    if not matches:
        return f"no matches for `{pattern}` under {path}"
    return "\n".join(matches)


def tool_find_function(workspace: Path, name: str, path: str = ".") -> str:
    """Find Rust function definition by name and return body with line numbers."""
    from audit_pipeline.utils.code_extract import extract_function

    p = _normalize_path(workspace, path)
    if p is None or not p.exists():
        return f"ERROR: path not found: {path}"
    files = [p] if p.is_file() else list(p.rglob("*.rs"))
    for f in files:
        result = extract_function(f, name, max_lines=200)
        if result:
            return f"--- {name} from {f.name} ---\n{result}"
    return f"function `{name}` not found under {path}"


# ---------------------------------------------------------------------------
# Tool-using agent loop
# ---------------------------------------------------------------------------


TOOLS_SCHEMA = [
    {
        "name": "read_file",
        "description": (
            "Read a Rust source file and return its contents with line numbers. "
            "Use this when you need to see the actual implementation of a function, "
            "struct, or constant. Always cite specific line numbers from the output "
            "in your final verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (workspace-relative or absolute)"},
                "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                "end_line": {"type": "integer", "description": "Last line to read (default: start+250)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search files for a regex pattern. Use this to find call sites, "
            "function definitions, struct usages, or constant references "
            "before reading the full file. Returns up to 50 matches as "
            "file:line: content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Directory or file (workspace-relative)"},
                "max_matches": {"type": "integer", "description": "Max matches (default 50)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_function",
        "description": (
            "Find a Rust function definition by name and return its body with "
            "line numbers. Faster than read_file when you know the function name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Function identifier"},
                "path": {"type": "string", "description": "Directory or file (default: workspace)"},
            },
            "required": ["name"],
        },
    },
]


def run_tool_using_agent(
    workspace: Path,
    system_prompt: str,
    initial_user_message: str,
    *,
    model: str = "claude-sonnet-4-6",
    max_turns: int = 20,
    max_tokens_per_turn: int = 8192,
) -> ToolUsingResult:
    """Run a tool-using Claude agent loop.

    Loops: agent calls a tool, we run it, agent gets result, agent calls
    another tool or produces a final text answer. Stops when the agent
    emits no more tool_use blocks or max_turns is reached.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY required for tool-using agent")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK required: pip install anthropic") from e

    client = anthropic.Anthropic(timeout=600)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message},
    ]
    total_in = 0
    total_out = 0
    n_turns = 0
    tool_calls_log: list[dict[str, Any]] = []
    stop_reason = "max_turns"

    while n_turns < max_turns:
        n_turns += 1
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens_per_turn,
            system=system_prompt,
            tools=TOOLS_SCHEMA,
            messages=messages,
        )
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        stop_reason = resp.stop_reason or "unknown"

        # Append assistant message
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            break

        # Process tool calls
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                tool_name = block.name
                tool_input = block.input or {}
                tool_calls_log.append({"tool": tool_name, "input": tool_input})
                try:
                    if tool_name == "read_file":
                        result = tool_read_file(
                            workspace,
                            tool_input.get("path", ""),
                            tool_input.get("start_line", 1),
                            tool_input.get("end_line"),
                        )
                    elif tool_name == "grep":
                        result = tool_grep(
                            workspace,
                            tool_input.get("pattern", ""),
                            tool_input.get("path", "."),
                            tool_input.get("max_matches", 50),
                        )
                    elif tool_name == "find_function":
                        result = tool_find_function(
                            workspace,
                            tool_input.get("name", ""),
                            tool_input.get("path", "."),
                        )
                    else:
                        result = f"ERROR: unknown tool {tool_name}"
                except Exception as e:  # noqa: BLE001
                    result = f"ERROR running tool: {e}"
                # Truncate very large results to keep context manageable
                if len(result) > 30000:
                    result = result[:30000] + "\n... [TRUNCATED]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})

    # Final text answer
    final_text = ""
    last_msg = messages[-1] if messages[-1]["role"] == "assistant" else None
    if last_msg:
        for block in last_msg["content"]:
            if hasattr(block, "text"):
                final_text += block.text

    return ToolUsingResult(
        text=final_text,
        input_tokens=total_in,
        output_tokens=total_out,
        n_turns=n_turns,
        tool_calls=tool_calls_log,
        stop_reason=stop_reason,
    )
