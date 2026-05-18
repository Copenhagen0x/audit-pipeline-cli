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


def _workspace_engine_roots(workspace: Path) -> list[Path]:
    """Read workspace.json and return resolved engine.local + wrapper.local
    dirs (if any). These are trusted source roots — the recon agent
    MUST be able to read them, even when they live outside
    ``workspace`` and outside ``/root/audit_runs``.

    Returns an empty list if workspace.json is missing/unparseable.
    Cached per-workspace via a tiny module-level dict to keep tool
    calls cheap.
    """
    cache = getattr(_workspace_engine_roots, "_cache", {})
    key = str(workspace)
    if key in cache:
        return cache[key]
    import json as _json
    out: list[Path] = []
    cfg_path = workspace / "workspace.json"
    if cfg_path.is_file():
        try:
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
            for k in ("engine", "wrapper"):
                local = (cfg.get(k) or {}).get("local")
                if local:
                    try:
                        p = (workspace / local).resolve()
                        if p.is_dir():
                            out.append(p)
                    except OSError:
                        pass
        except (_json.JSONDecodeError, OSError):
            pass
    cache[key] = out
    _workspace_engine_roots._cache = cache  # type: ignore[attr-defined]
    return out


def _normalize_path(workspace: Path, path: str) -> Path | None:
    """Resolve a tool-supplied path, refusing escapes outside workspace.

    Trusted prefixes:
      1. ``workspace`` itself
      2. the audit_runs root (env-var overridable via vps_paths)
      3. ``engine.local`` + ``wrapper.local`` declared in
         workspace.json — required for non-Solana OSec workspaces
         whose engine clone lives under a sibling path like
         ``/root/audit_runs/<eval>/repos/<lang>-<size>/``

    Resolution order:
      a. absolute path → use as-is
      b. workspace / path → if that exists, use it
      c. engine_root / path → if any engine_root has it, use that
         (so agents can say "sources/X.move" without the engine prefix)
    """
    from audit_pipeline.utils.vps_paths import is_under_trusted_root

    try:
        ws = workspace.resolve()
    except OSError:
        return None

    candidates: list[Path] = []
    if Path(path).is_absolute():
        try:
            candidates.append(Path(path).resolve())
        except OSError:
            return None
    else:
        # Try workspace-relative first
        try:
            candidates.append((workspace / path).resolve())
        except OSError:
            pass
        # Then try each engine_root + several common source-prefixes
        # as fallbacks. Agents inconsistently call read_file with:
        #   - "sources/access_control.move"  (works under engine_root)
        #   - "access_control.move"           (bare — needs sources/ prefix)
        #   - "src/foo.c"                     (works under engine_root)
        #   - "foo.c"                         (bare C — needs src/ prefix)
        # This was caught during the 2026-05-13 aptos-small dry-run when
        # 3 of 8 hyps came back INCONCLUSIVE because the agent used bare
        # filenames the resolver couldn't find. Now we try every plausible
        # prefix so bare-named requests resolve when the file exists.
        _SOURCE_PREFIXES = ("", "sources/", "src/", "contracts/", "programs/")
        for engine_root in _workspace_engine_roots(workspace):
            for prefix in _SOURCE_PREFIXES:
                try:
                    candidates.append((engine_root / prefix / path).resolve())
                except OSError:
                    pass

    # Pick the first candidate that EXISTS and is under a trusted root.
    # Falling back to first candidate (existence-unchecked) if nothing
    # exists — caller's .is_file() check will emit the right error.
    fallback: Path | None = None
    for p in candidates:
        p_str = str(p)
        trusted = (
            p_str.startswith(str(ws))
            or is_under_trusted_root(p)
            or any(p_str.startswith(str(r)) for r in _workspace_engine_roots(workspace))
        )
        if not trusted:
            continue
        if fallback is None:
            fallback = p
        if p.exists():
            return p
    return fallback


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
    _EXTS = (
        ".rs", ".md",
        ".move",
        ".sol",
        ".c", ".h", ".cpp", ".cc", ".hpp",
        ".toml", ".json", ".yaml", ".yml",
    )
    files: list[Path] = []
    if p.is_file():
        files = [p]
    else:
        for ext in _EXTS:
            files.extend(p.rglob(f"*{ext}"))
        # rglob() does NOT follow symlinks by default. OSec workspaces
        # use a symlinked `engine` dir → without explicit engine_root
        # traversal, grep on workspace root returned ZERO .move files
        # and the agent concluded "the repo is empty" → INCONCLUSIVE/LOW
        # verdicts. Explicitly iterate every engine_root declared in
        # workspace.json so symlinked source trees ARE searched.
        try:
            p_resolved = p.resolve()
        except OSError:
            p_resolved = p
        for engine_root in _workspace_engine_roots(workspace):
            try:
                er_resolved = engine_root.resolve()
            except OSError:
                er_resolved = engine_root
            # Only add if engine_root isn't already under p_resolved
            # (avoid double-scanning when workspace is the engine root)
            if str(er_resolved).startswith(str(p_resolved)):
                continue
            # Iterate ALL files matching ext directly via os.walk
            # (handles symlinked targets reliably across Python versions).
            import os as _os
            for root, dirs, fnames in _os.walk(er_resolved, followlinks=True):
                # Skip vendor / .git noise
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target", "build")]
                for fn in fnames:
                    if any(fn.endswith(ext) for ext in _EXTS):
                        files.append(Path(root) / fn)
    # Display path relative to whichever root the file lives under
    # (workspace OR engine_root), so the agent sees readable paths
    # like "sources/auction.move" instead of bare filenames.
    def _display_path(f: Path) -> str:
        try:
            if f.is_relative_to(workspace):
                return str(f.relative_to(workspace))
        except (AttributeError, ValueError):
            pass
        for root in _workspace_engine_roots(workspace):
            try:
                if f.is_relative_to(root):
                    return str(f.relative_to(root))
            except (AttributeError, ValueError):
                continue
        return f.name

    matches: list[str] = []
    seen_files: set[str] = set()
    for f in files:
        # Dedupe (engine_root walk + workspace rglob may double-list
        # if user has manually copied source into workspace)
        fkey = str(f.resolve()) if f.exists() else str(f)
        if fkey in seen_files:
            continue
        seen_files.add(fkey)
        try:
            for ln, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if rx.search(line):
                    matches.append(f"{_display_path(f)}:{ln}: {line}")
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
    """Find a function definition by name and return body with line numbers.

    Language-aware: walks every supported source extension (.rs, .c/.h,
    .move, .sol). `extract_function` infers the language per-file from
    extension and applies the right function-start regex.
    """
    from audit_pipeline.utils.code_extract import extract_function

    p = _normalize_path(workspace, path)
    if p is None or not p.exists():
        return f"ERROR: path not found: {path}"

    _FUNC_EXTS = (".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".move", ".sol")
    if p.is_file():
        files: list[Path] = [p]
    else:
        files = []
        for ext in _FUNC_EXTS:
            files.extend(p.rglob(f"*{ext}"))
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
            "Read a source file and return its contents with line numbers. "
            "Works for Rust (.rs), C (.c/.h/.cc/.cpp/.hpp), Move (.move), "
            "and Solidity (.sol). Use this when you need to see the actual "
            "implementation of a function, struct, or constant. Always cite "
            "specific line numbers from the output in your final verdict."
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
            "file:line: content. Searches across all source extensions of "
            "the target language."
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
            "Find a function definition by name and return its body with "
            "line numbers. Language-aware — matches `fn name(` in Rust, "
            "C-style `[static] return_type name(` in C, `fun name` in Move, "
            "and `function name` in Solidity. Faster than read_file when "
            "you know the function name."
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
    hyp_id: str = "",
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
                # Live-feed the tool call to subscribed customer dashboards
                # (the Bridge view's tool-call stream + hypothesis grid
                # "thinking" animations). Best-effort; never raises.
                #
                # POST-AUDIT FIX: previously read hyp_id from a process-global
                # env var (JELLEO_ACTIVE_HYP_ID), which races across
                # concurrent ThreadPoolExecutor workers — tool_call events
                # got cross-attributed. Now passed in as a per-call kwarg.
                try:
                    from audit_pipeline.utils.event_log import emit_event
                    emit_event(
                        "tool_call",
                        hyp_id=hyp_id,
                        tool=tool_name,
                        path=str(tool_input.get("path", ""))[:200],
                        pattern=str(tool_input.get("pattern", ""))[:200],
                        name=str(tool_input.get("name", ""))[:200],
                        start_line=tool_input.get("start_line"),
                        end_line=tool_input.get("end_line"),
                        turn=n_turns,
                    )
                except Exception:  # noqa: BLE001
                    pass
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

    # If we hit max_turns mid-tool-use, force a final verdict turn.
    if stop_reason == "tool_use" and n_turns >= max_turns:
        messages.append({
            "role": "user",
            "content": (
                "Maximum exploration turns reached. STOP using tools now and "
                "render your FINAL VERDICT based on what you've already found. "
                "Cite specific file paths and line numbers from your prior "
                "tool results. End with the required `## Verdict` section."
            ),
        })
        try:
            forced_resp = client.messages.create(
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system_prompt,
                tools=TOOLS_SCHEMA,
                messages=messages,
            )
            total_in += forced_resp.usage.input_tokens
            total_out += forced_resp.usage.output_tokens
            stop_reason = forced_resp.stop_reason or stop_reason
            messages.append({"role": "assistant", "content": forced_resp.content})
            n_turns += 1
        except Exception:  # noqa: BLE001
            pass

    # Pull text from the LAST assistant message that has any text content
    final_text = ""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        text_parts = []
        for block in msg["content"]:
            if hasattr(block, "text") and block.text:
                text_parts.append(block.text)
        if text_parts:
            final_text = "\n\n".join(text_parts)
            break

    if not final_text:
        final_text = (
            f"[NO FINAL TEXT — agent made {len(tool_calls_log)} tool calls "
            f"across {n_turns} turns but did not produce a verdict]"
        )

    return ToolUsingResult(
        text=final_text,
        input_tokens=total_in,
        output_tokens=total_out,
        n_turns=n_turns,
        tool_calls=tool_calls_log,
        stop_reason=stop_reason,
    )
