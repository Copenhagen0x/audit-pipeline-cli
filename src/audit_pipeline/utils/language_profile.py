"""Per-language profiles for prompt rendering, tool selection, and grounding.

Every layer that ships English text to an LLM (L1 recon, L1.5 debate,
L2.5 triage, L3 formal authoring) needs to know what language the
target codebase is in so the framing, file extensions, formal-tool
name, runtime-tool name, and idiom set match reality.

Historically the prompts hardcoded Solana / Rust / Kani / LiteSVM
because Percolator was the only target. When we added aptos and
solidity cells, the language-specific *system* prompt got swapped in
but the *user* prompt body (loaded from agent_prompts/*.md) kept
saying "Solana program... .rs files... Kani..." — the agents worked
through the contradiction but it wasted tokens and degraded quality.

This module is the single source of truth. Every consumer formats
prompt templates against `LANGUAGE_PROFILES[language]` so the rendered
text reflects the target language end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    display_name: str           # human-readable name for prompt prose
    program_kind: str           # "program", "smart contract", etc.
    source_exts: tuple[str, ...]   # extensions to glob under engine src/
    primary_ext: str               # extension for `find_function` walk
    formal_tool: str               # "Kani", "CBMC", "Move Prover", "Halmos"
    runtime_tool: str              # "LiteSVM", "AFL++", "aptos move test", "forge fuzz"
    poc_idiom: str                 # cmd shorthand: "cargo test", "clang+ASan", ...
    function_def_regex: str        # python regex for extract_function (named group `body` after `{`)
    assertion_idiom: str           # short list of in-language assertion patterns
    entry_point_label: str         # "BPF instructions", "public entry functions", ...
    src_dir_path: str              # canonical source root, relative to engine: "src/" or "sources/"
    architecture_summary: str      # 1-2 line architectural blurb for orientation prompts
    manifest_file: str             # "Cargo.toml", "Makefile / build.sh", "Move.toml", "foundry.toml"


# extract_function regex notes:
#   * Each pattern uses a single `(?P<name>...)` capture for the function
#     name and a single `(?P<open>\{)` capture for the opening brace
#     (`extract_function` walks the body using brace depth from there).
#   * Patterns are MULTILINE-friendly: leading whitespace is absorbed.

_RUST_FUNC_RE = (
    r"(?m)^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"fn\s+(?P<name>{NAME})\s*(?:<[^>]*>)?\s*\([^)]*\)[^{{]*(?P<open>\{)"
)

_C_FUNC_RE = (
    r"(?m)^\s*(?:static\s+|inline\s+|extern\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_\*\s]*?\s+\*?\s*)"
    r"(?P<name>{NAME})\s*\([^)]*\)\s*(?P<open>\{)"
)

_MOVE_FUNC_RE = (
    r"(?m)^\s*(?:public(?:\s*\([^)]*\))?\s+)?(?:entry\s+)?(?:native\s+)?"
    r"fun\s+(?P<name>{NAME})\s*(?:<[^>]*>)?\s*\([^)]*\)[^{{]*(?P<open>\{)"
)

_SOLIDITY_FUNC_RE = (
    r"(?m)^\s*(?:function\s+)(?P<name>{NAME})\s*\([^)]*\)[^{{]*(?P<open>\{)"
)


LANGUAGE_PROFILES: dict[str, LanguageProfile] = {
    "solana": LanguageProfile(
        name="solana",
        display_name="Solana / Rust",
        program_kind="Solana program (Rust engine + BPF wrapper)",
        source_exts=(".rs",),
        primary_ext=".rs",
        formal_tool="Kani",
        runtime_tool="LiteSVM",
        poc_idiom="cargo test",
        function_def_regex=_RUST_FUNC_RE,
        assertion_idiom="`assert!`, `debug_assert!`, or early `return Err(...)`",
        entry_point_label="BPF instructions",
        src_dir_path="src/",
        architecture_summary="Rust engine (library) + BPF wrapper (program entrypoints)",
        manifest_file="Cargo.toml",
    ),
    "c": LanguageProfile(
        name="c",
        display_name="C / systems software",
        program_kind="C program (systems software)",
        source_exts=(".c", ".h"),
        primary_ext=".c",
        formal_tool="CBMC (Bounded Model Checker)",
        runtime_tool="AFL++ (coverage-guided fuzzer)",
        poc_idiom="clang + ASan/UBSan/SignedOverflowSan",
        function_def_regex=_C_FUNC_RE,
        assertion_idiom="`assert(...)`, early `return -1;` / `return NULL;`, or explicit error variant",
        entry_point_label="entry functions / event handlers",
        src_dir_path="src/",
        architecture_summary=(
            "Plain C program. Files under `src/` (recursive — auth/, "
            "common/, vendor/, etc.). No VM, no smart-contract framing."
        ),
        manifest_file="Makefile / build.sh",
    ),
    "aptos": LanguageProfile(
        name="aptos",
        display_name="Aptos Move",
        program_kind="Aptos Move smart-contract module",
        source_exts=(".move",),
        primary_ext=".move",
        formal_tool="Move Prover",
        runtime_tool="aptos move test (property-based)",
        poc_idiom="aptos move test (#[test] annotations)",
        function_def_regex=_MOVE_FUNC_RE,
        assertion_idiom="`assert!(...)`, `abort <code>`, or explicit error variant",
        entry_point_label="public entry functions",
        src_dir_path="sources/",
        architecture_summary=(
            "Aptos Move module. Resources are linear (no copy/drop); "
            "borrow_global / borrow_global_mut is an explicit auth gate; "
            "capabilities (key/store abilities) gate privileged ops."
        ),
        manifest_file="Move.toml",
    ),
    "solidity": LanguageProfile(
        name="solidity",
        display_name="Solidity / EVM",
        program_kind="Solidity smart-contract (EVM, Foundry framework)",
        source_exts=(".sol",),
        primary_ext=".sol",
        formal_tool="Halmos (symbolic execution)",
        runtime_tool="forge fuzz / forge invariant",
        poc_idiom="forge test (Foundry)",
        function_def_regex=_SOLIDITY_FUNC_RE,
        assertion_idiom="`require(...)`, `revert(...)`, custom `error` types",
        entry_point_label="external / public functions",
        src_dir_path="src/",
        architecture_summary=(
            "Solidity 0.8+ contracts (Foundry). Storage layout, modifier "
            "chains, and external/public entrypoints define the auth and "
            "value-flow surface."
        ),
        manifest_file="foundry.toml",
    ),
}


def profile_for(language: str | None) -> LanguageProfile:
    """Return the profile for ``language`` (case-insensitive).

    Falls back to the Solana profile for unknown / empty languages —
    this preserves the legacy Percolator behavior any caller that
    doesn't yet pass a language tag.
    """
    key = (language or "").strip().lower()
    return LANGUAGE_PROFILES.get(key, LANGUAGE_PROFILES["solana"])


def render_template(template_text: str, language: str | None) -> str:
    """Substitute language-profile placeholders in ``template_text``.

    Recognized placeholders:

      * ``{LANGUAGE_DISPLAY}``     — display_name
      * ``{PROGRAM_KIND}``         — program_kind
      * ``{SOURCE_EXTS}``          — comma-joined exts (e.g. ``.c, .h``)
      * ``{PRIMARY_EXT}``          — single primary ext (e.g. ``.rs``)
      * ``{FORMAL_TOOL}``          — formal_tool
      * ``{RUNTIME_TOOL}``         — runtime_tool
      * ``{POC_IDIOM}``            — poc_idiom
      * ``{ASSERTION_IDIOM}``      — assertion_idiom
      * ``{ENTRY_POINT_LABEL}``    — entry_point_label
      * ``{SRC_DIR_PATH}``         — src_dir_path
      * ``{ARCHITECTURE_SUMMARY}`` — architecture_summary
      * ``{MANIFEST_FILE}``        — manifest_file

    Any pre-existing ``{ENGINE_PATH}`` / ``{ENGINE_REPO_URL}`` etc.
    placeholders are left untouched — callers fill those in separately
    against the workspace.
    """
    p = profile_for(language)
    substitutions = {
        "LANGUAGE_DISPLAY":     p.display_name,
        "PROGRAM_KIND":         p.program_kind,
        "SOURCE_EXTS":          ", ".join(p.source_exts),
        "PRIMARY_EXT":          p.primary_ext,
        "FORMAL_TOOL":          p.formal_tool,
        "RUNTIME_TOOL":         p.runtime_tool,
        "POC_IDIOM":            p.poc_idiom,
        "ASSERTION_IDIOM":      p.assertion_idiom,
        "ENTRY_POINT_LABEL":    p.entry_point_label,
        "SRC_DIR_PATH":         p.src_dir_path,
        "ARCHITECTURE_SUMMARY": p.architecture_summary,
        "MANIFEST_FILE":        p.manifest_file,
    }
    out = template_text
    for k, v in substitutions.items():
        out = out.replace("{" + k + "}", v)
    return out
