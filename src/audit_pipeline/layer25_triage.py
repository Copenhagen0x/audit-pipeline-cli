"""Layer 2.5 — automated fire triage between Layer 2 (PoC) and Layer 3 (Kani).

Cycle 20260511-183154 produced 64 PoC fires from 320 tested hyps (~20% raw
fire rate). Manual triage by Claude collapsed them into:
    7 STRONG (4 distinct root causes)
   11 SOFT (test mis-frames the claim / by-design / wrong layer)
   45 FALSE (PoC infra panic — 41 of those were the *same* `RiskParams:
            Overflow` panic from a broken params_for_*() factory)

Without this stage, Layer 3 + Layer 4 would have been called on ALL 64
fires (~$326–685 of Kani+LiteSVM spend, ~$280 of which would be wasted
on FALSE/SOFT fires).

This module is the productized form of that manual judgment. Two layers:

  1. FALSE_PATTERNS: regex/substring patterns matched against the cargo
     panic line. These catch the dominant "PoC factory panicked before
     the assertion fired" failure mode with no LLM call. Cycle 20260511
     would have collapsed 41/64 fires here at zero cost.

  2. LLM judge: for fires that DIDN'T match the fast-path, send the
     (claim, test body, panic line, engine source for claimed function)
     to an LLM judge which returns STRONG / SOFT / FALSE / LOST + reason.

STRONG fires are then clustered by root cause (bug_class + engine_function
+ claim-shingle similarity). Each cluster gets ONE representative dispatched
to Layer 3; the others are recorded as covered-by-cluster.

Output: ``triage.jsonl`` in the cycle dir, one row per fire, plus a
``layer3_dispatch_set`` exposed to the caller as the filtered set of hyp_ids.

Auto-mode is opt-in via the hunt CLI's ``--triage-fires`` flag (default ON
in hunt; default OFF when called as a CLI subcommand on a finished cycle).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Fast-path FALSE patterns (no LLM cost)
# ---------------------------------------------------------------------------
# Each entry: (pattern, reason). Matched against the cargo panic line.
# Adding a pattern means appending here — these are well-documented
# false-fire signatures the engine has seen before. New patterns should
# be added with a comment citing the cycle / hypothesis_id where the
# signature was first observed.

FALSE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Cycle 20260511 — 41/45 FALSE fires were this exact panic from
        # a broken `params_for_*()` factory in the PoC scaffolding. The
        # test never reached the actual claim — the helper factory unwrapped
        # an Err during construction.
        re.compile(r"invalid\s+RiskParams\s*:\s*Overflow", re.IGNORECASE),
        "PoC factory panicked on RiskParams construction "
        "(broken params_for_*() helper, NOT the engine claim)",
    ),
    (
        # Setup-side `let x = ... .unwrap();` where the constructor returned
        # Err. Match any error-shaped variant after the unwrap-on-Err prefix.
        # Examples: `EngineInsufficientBalance`, `HyperpTradeNoCpiDisabled`,
        # `PercolatorError::...`, `InvalidInstructionData`.
        re.compile(
            r"called\s+`Result::unwrap\(\)`\s+on\s+an\s+`Err`\s+value:",
            re.IGNORECASE,
        ),
        "setup helper unwrap() on engine constructor error — test "
        "never reached the assertion",
    ),
    (
        re.compile(
            r"attempt to (subtract with overflow|add with overflow|"
            r"multiply with overflow)",
            re.IGNORECASE,
        ),
        "raw arithmetic overflow in test setup math — not the claim "
        "(claims about engine overflow should fire engine code, not test setup)",
    ),
    (
        # Order-independent: panic location is in a tests/test_*.rs file
        # AND the panic message is `index out of bounds`. Either ordering
        # ("tests/test_X.rs ... index out of bounds" or
        #  "index out of bounds at tests/test_X.rs") matches.
        re.compile(
            r"(?=.*test_\w+\.rs)(?=.*index out of bounds)",
            re.IGNORECASE | re.DOTALL,
        ),
        "out-of-bounds in the test file itself (test bug, not engine bug)",
    ),
    (
        # Common test-side assertion failures that have nothing to do with
        # the engine state under test
        re.compile(
            r"assertion `left\s*[!=]=\s*right`\s+failed.*\b(setup|init|construct|build)\w*\b",
            re.IGNORECASE,
        ),
        "assertion failure in test-side setup phase (env construction, "
        "not the claim being tested)",
    ),
)


@dataclass
class TriageResult:
    hyp_id: str
    classification: str  # STRONG | SOFT | FALSE | LOST
    reason: str
    # Set on STRONG; None for SOFT/FALSE/LOST.
    cluster_id: str | None = None
    is_representative: bool = False
    # Whether the LLM judge was actually called (vs fast-path).
    used_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pattern-match fast path
# ---------------------------------------------------------------------------


def classify_by_pattern(panic_line: str) -> tuple[str, str] | None:
    """Return (classification, reason) if any FALSE_PATTERN matches, else None.

    Always returns the FIRST match — order in FALSE_PATTERNS is precedence.
    """
    if not panic_line:
        return None
    for pat, reason in FALSE_PATTERNS:
        if pat.search(panic_line):
            return ("FALSE", reason)
    return None


def extract_panic_line(cargo_log: str) -> str:
    """Pull the panic line(s) out of a cargo test log.

    Returns the first ``panicked at`` line + the next line (which is
    usually the assertion message). Returns "" if no panic detected.
    """
    if not cargo_log:
        return ""
    lines = cargo_log.splitlines()
    for i, line in enumerate(lines):
        if "panicked at" in line.lower():
            # Include the next non-empty line if present (assertion msg)
            tail = lines[i + 1] if i + 1 < len(lines) else ""
            return (line + " " + tail).strip()
    # No explicit panic — look for `assertion ... failed` as fallback
    for i, line in enumerate(lines):
        if re.search(r"assertion.*failed", line, re.IGNORECASE):
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# LLM judge (called only for fires that didn't fast-path)
# ---------------------------------------------------------------------------


JUDGE_SYSTEM_PROMPT = """\
You are a Solana security audit triage judge. You receive ONE PoC fire
that the L2 layer reported as a "test failed - bug reproduced." Your job
is to classify the fire into exactly one of:

  STRONG - The assertion message references the actual semantic invariant
           the hypothesis claims. The test exercises the claim path.
           Worth promoting to formal verification (Kani / LiteSVM).
  SOFT   - The test fires, but for a DIFFERENT reason than the claim:
             * test mis-frames the claim (wrong API, wrong precondition)
             * the panic is by-design (engine escape hatch, error path)
             * wrong layer of abstraction (engine fires; wrapper handles)
  FALSE  - The fire is PoC infrastructure error: params factory panicked,
           setup helper unwrapped on an Err, test setup arithmetic
           overflow, etc. The test never reached the claim assertion.

Return JSON only. Schema:
  {"classification": "STRONG"|"SOFT"|"FALSE",
   "reason": "<one-sentence explanation>"}

Do not add commentary outside the JSON.
"""


def build_judge_user_prompt(
    hyp_id: str,
    claim: str,
    bug_class: str,
    engine_function: str,
    test_body: str,
    panic_line: str,
    engine_source: str = "",
) -> str:
    blocks = [
        f"# Hypothesis {hyp_id}",
        f"Claim: {claim}",
        f"Bug class: {bug_class}",
        f"Engine function: {engine_function}",
        "",
        "## Panic line + assertion",
        f"```\n{panic_line[:1500]}\n```",
        "",
        "## Test body (the PoC)",
        f"```rust\n{test_body[:4000]}\n```",
    ]
    if engine_source:
        blocks += [
            "",
            "## Engine source for the claimed function",
            f"```rust\n{engine_source[:3000]}\n```",
        ]
    blocks += [
        "",
        "Return the classification JSON now.",
    ]
    return "\n".join(blocks)


def _parse_judge_response(text: str) -> tuple[str, str]:
    """Extract (classification, reason) from a judge response. Robust to
    surrounding prose if the model decided to chat anyway."""
    # Find first JSON-looking object
    m = re.search(r"\{[^{}]*\"classification\"[^{}]*\}", text, re.DOTALL)
    if not m:
        return ("SOFT", "judge response had no parseable JSON (defaulting to SOFT)")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ("SOFT", "judge JSON unparseable (defaulting to SOFT)")
    cls = str(d.get("classification", "")).upper().strip()
    if cls not in ("STRONG", "SOFT", "FALSE"):
        return ("SOFT", f"judge returned unknown classification {cls!r} (defaulting to SOFT)")
    reason = str(d.get("reason") or "")[:300]
    return (cls, reason)


def judge_one(
    hyp_id: str,
    claim: str,
    bug_class: str,
    engine_function: str,
    test_body: str,
    panic_line: str,
    engine_source: str = "",
    *,
    model: str | None = None,
    complete_fn=None,  # injection seam for tests
) -> tuple[str, str]:
    """LLM judge call. Returns (classification, reason).

    ``complete_fn`` is an injection seam so tests can stub the LLM. If
    not provided, uses ``audit_pipeline.utils.complete``.
    """
    if complete_fn is None:
        from audit_pipeline.utils import complete as _real_complete
        complete_fn = _real_complete
    user_prompt = build_judge_user_prompt(
        hyp_id, claim, bug_class, engine_function,
        test_body, panic_line, engine_source,
    )
    try:
        if model:
            resp = complete_fn(user_prompt, system=JUDGE_SYSTEM_PROMPT, model=model)
        else:
            resp = complete_fn(user_prompt, system=JUDGE_SYSTEM_PROMPT)
    except Exception as e:  # noqa: BLE001
        return ("SOFT", f"judge call failed ({type(e).__name__}: {e!s:.150}); defaulting to SOFT")
    return _parse_judge_response(getattr(resp, "text", str(resp)))


# ---------------------------------------------------------------------------
# Root-cause clustering (STRONG fires only)
# ---------------------------------------------------------------------------


def _claim_shingles(claim: str, k: int = 3) -> set[tuple[str, ...]]:
    toks = (claim or "").lower().split()
    return {tuple(toks[i:i + k]) for i in range(max(0, len(toks) - k + 1))}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_strong_fires(
    strong: list[dict[str, Any]],
    *,
    similarity_threshold: float = 0.25,
) -> dict[str, list[str]]:
    """Cluster STRONG fires by root cause.

    Returns ``{cluster_id: [hyp_id, ...]}`` where the cluster_id is the
    FIRST hyp_id added to the cluster.

    Membership rule (in priority order):

      1. **Strong match — same engine_function**: if two STRONG fires
         hit the SAME engine_function with the SAME bug_class, they're
         the same root cause by definition. The LLM judge already
         confirmed both are STRONG (real bugs); two real bugs in the
         same function under the same bug class is one root cause
         expressed twice. Cluster regardless of claim wording.

      2. **Weak match — claim similarity**: if one of bug_class /
         engine_function is missing, fall back to comparing claim
         3-token shingle Jaccard against any prior cluster member.
         Compared to ALL members (not just the cluster representative)
         so claim drift within a cluster doesn't split it apart.

    Cycle 20260511 produced 7 STRONG fires that clustered to 4 distinct
    root causes via this rule (F7 family = 4 hyps in
    ``use_insurance_buffer`` → 1 cluster; AR7 / CI10 / L3 each in their
    own singleton cluster).
    """
    clusters: dict[str, list[str]] = {}
    cluster_meta: dict[str, list[dict[str, Any]]] = {}  # cluster_id → [member metas]

    for fire in strong:
        hyp_id = fire["hyp_id"]
        bc = (fire.get("bug_class") or "").strip().lower()
        ef = (fire.get("engine_function") or "").strip().lower()
        sh = _claim_shingles(fire.get("claim") or "")
        meta = {"bug_class": bc, "engine_function": ef, "shingles": sh}

        matched: str | None = None
        for cid, members in cluster_meta.items():
            # Rule 1: strong match — same bug_class AND engine_function.
            # Two STRONG fires in the same function under the same bug
            # class are one root cause expressed twice.
            if bc and ef:
                if any(m["bug_class"] == bc and m["engine_function"] == ef
                       for m in members):
                    matched = cid
                    break
            # Rule 2: weak match — claim shingle similarity, but ONLY if
            # bug_class agrees AND engine_function isn't a hard mismatch.
            # Different engine_function = different code path = different
            # root cause, even when the claim wording rhymes. This rule
            # only kicks in when at least one side has no engine_function
            # set (e.g. legacy hyps that pre-date the field).
            engine_compatible = (not ef) or any(
                (not m["engine_function"]) or m["engine_function"] == ef
                for m in members
            )
            if engine_compatible and any(
                m["bug_class"] == bc
                and _jaccard(sh, m["shingles"]) >= similarity_threshold
                for m in members
            ):
                matched = cid
                break

        if matched is None:
            clusters[hyp_id] = [hyp_id]
            cluster_meta[hyp_id] = [meta]
        else:
            clusters[matched].append(hyp_id)
            cluster_meta[matched].append(meta)
    return clusters


# ---------------------------------------------------------------------------
# End-to-end triage for a whole cycle
# ---------------------------------------------------------------------------


def triage_cycle(
    cycle_dir: Path,
    *,
    poc_results: dict[str, dict[str, Any]],
    hyp_meta: dict[str, dict[str, Any]],
    engine_src_loader=None,
    complete_fn=None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """Triage every fired PoC in a cycle.

    Returns:
      {
        "results": [TriageResult.to_dict() per fire],
        "layer3_dispatch_set": [hyp_id, ...],  # STRONG representatives only
        "counts": {"STRONG": n, "SOFT": n, "FALSE": n, "LOST": n},
        "n_llm_calls": int,
        "triage_jsonl_path": str,
      }

    ``engine_src_loader`` is a callable ``(engine_function: str) -> str``
    that returns the source text for the claimed function. If None, the
    judge prompt is built without the engine-source block (slightly weaker
    judgments but works in offline / test contexts).
    """
    fired_hyp_ids = sorted([
        hid for hid, pr in poc_results.items() if pr.get("fired")
    ])
    results: list[TriageResult] = []
    n_llm_calls = 0

    for hyp_id in fired_hyp_ids:
        poc = poc_results.get(hyp_id, {})
        meta = hyp_meta.get(hyp_id, {})

        # Read test body + cargo log
        scaffold_path = poc.get("scaffold_path")
        cargo_log_path = poc.get("cargo_log_path")
        test_body = ""
        if scaffold_path:
            try:
                test_body = Path(scaffold_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        cargo_log = ""
        if cargo_log_path:
            try:
                cargo_log = Path(cargo_log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        # LOST: test file or log missing
        if not test_body or not cargo_log:
            results.append(TriageResult(
                hyp_id=hyp_id, classification="LOST",
                reason=(
                    f"test_body present: {bool(test_body)}, "
                    f"cargo_log present: {bool(cargo_log)}"
                ),
            ))
            continue

        panic_line = extract_panic_line(cargo_log)

        # Fast-path FALSE
        fast = classify_by_pattern(panic_line)
        if fast is not None:
            cls, reason = fast
            results.append(TriageResult(
                hyp_id=hyp_id, classification=cls, reason=reason,
                used_llm=False,
            ))
            continue

        # LLM judge for non-obvious cases
        engine_function = meta.get("engine_function") or ""
        engine_source = ""
        if engine_src_loader and engine_function:
            try:
                engine_source = engine_src_loader(engine_function) or ""
            except Exception:  # noqa: BLE001
                engine_source = ""

        cls, reason = judge_one(
            hyp_id,
            claim=meta.get("claim", ""),
            bug_class=meta.get("bug_class", ""),
            engine_function=engine_function,
            test_body=test_body,
            panic_line=panic_line,
            engine_source=engine_source,
            model=judge_model,
            complete_fn=complete_fn,
        )
        n_llm_calls += 1
        results.append(TriageResult(
            hyp_id=hyp_id, classification=cls, reason=reason, used_llm=True,
        ))

    # Cluster STRONG fires
    strong_dicts = [
        {
            "hyp_id": r.hyp_id,
            "bug_class": hyp_meta.get(r.hyp_id, {}).get("bug_class"),
            "engine_function": hyp_meta.get(r.hyp_id, {}).get("engine_function"),
            "claim": hyp_meta.get(r.hyp_id, {}).get("claim"),
        }
        for r in results if r.classification == "STRONG"
    ]
    clusters = cluster_strong_fires(strong_dicts)

    # Mark cluster membership + representatives on the result rows
    cluster_lookup: dict[str, str] = {}
    for cid, members in clusters.items():
        for hid in members:
            cluster_lookup[hid] = cid
    for r in results:
        if r.classification == "STRONG":
            r.cluster_id = cluster_lookup.get(r.hyp_id)
            r.is_representative = (r.cluster_id == r.hyp_id)

    # Write triage.jsonl
    triage_jsonl = cycle_dir / "triage.jsonl"
    triage_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with triage_jsonl.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), sort_keys=True) + "\n")

    counts = {
        "STRONG": sum(1 for r in results if r.classification == "STRONG"),
        "SOFT":   sum(1 for r in results if r.classification == "SOFT"),
        "FALSE":  sum(1 for r in results if r.classification == "FALSE"),
        "LOST":   sum(1 for r in results if r.classification == "LOST"),
    }
    layer3_dispatch_set = [
        r.hyp_id for r in results
        if r.classification == "STRONG" and r.is_representative
    ]

    return {
        "results": [r.to_dict() for r in results],
        "layer3_dispatch_set": layer3_dispatch_set,
        "clusters": clusters,
        "counts": counts,
        "n_llm_calls": n_llm_calls,
        "triage_jsonl_path": str(triage_jsonl),
    }


__all__ = [
    "FALSE_PATTERNS",
    "TriageResult",
    "build_judge_user_prompt",
    "classify_by_pattern",
    "cluster_strong_fires",
    "extract_panic_line",
    "judge_one",
    "triage_cycle",
]
