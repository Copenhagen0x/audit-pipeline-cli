"""Regression tests for _parse_verdict (recon.py).

Cycle 20260514-151541 caught a phantom hyp slipping through L2 selection:

  APT29-auction-settle-no-winner — codebase has no auction module
  proposer body: `## Verdict\\n\\n**FALSE** — Confidence: **HIGH**`
                 then: "The claim...cannot be TRUE or violated because..."
  parser output: TRUE                                  ← WRONG

Root cause: parser's last-resort fallback was a naive `\\bTRUE\\b` /
`\\bFALSE\\b` substring scan over the first 200 chars of the verdict
block. APT29's body contained "TRUE" inside the rhetorical
"cannot be TRUE or violated" phrase later in the block, but the parser
grabbed that occurrence before it reached the actual `**FALSE**`
declaration at the very start.

Fix: extract the verdict from the FIRST NON-EMPTY LINE of the block
via an anchored regex. "cannot be TRUE" is not at line-start, so it no
longer matches. Naive substring fallbacks were removed.
"""
from __future__ import annotations

from audit_pipeline.commands.recon import _parse_verdict


def test_parses_bolded_false_with_trailing_confidence() -> None:
    """The exact APT29 case — `**FALSE** — Confidence: **HIGH**` on the
    first line of the verdict block, with "cannot be TRUE" appearing
    later in the same block."""
    text = (
        "## Investigation\n\nlots of analysis here\n\n"
        "## Verdict\n\n"
        "**FALSE** — Confidence: **HIGH**\n\n"
        "The claim that every settle path handles the no-bid case "
        "correctly cannot be TRUE or violated because no auction "
        "module exists in the codebase.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "FALSE", (
        f"regression: APT29-style verdict parsed as {verdict} instead of FALSE — "
        "parser is grabbing 'TRUE' from 'cannot be TRUE' rhetorical phrase"
    )
    assert confidence == "HIGH"


def test_parses_bolded_true_with_trailing_confidence() -> None:
    text = (
        "## Verdict\n\n"
        "**TRUE** — HIGH confidence\n\n"
        "The hypothesis is confirmed at vault.move:42.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "TRUE"
    assert confidence == "HIGH"


def test_parses_naked_false_with_em_dash_confidence() -> None:
    """Many agents render verdict as `FALSE — Confidence: HIGH` without
    asterisks. The lead-line match should still catch it."""
    text = (
        "## Verdict\n\n"
        "FALSE — Confidence: HIGH\n\n"
        "Some elaboration.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "FALSE"
    assert confidence == "HIGH"


def test_inconclusive_in_first_line_returns_unknown() -> None:
    """Original tool-grounding-failed agents wrote `**INCONCLUSIVE**`.
    Treat as UNKNOWN so it doesn't slip into L2 as TRUE/FALSE."""
    text = (
        "## Verdict\n\n"
        "**INCONCLUSIVE** — Confidence: **LOW**\n\n"
        "Could not locate source files via tools.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "UNKNOWN"


def test_does_not_pick_up_rhetorical_true_in_false_body() -> None:
    """Adversarial case: FALSE verdict but body discusses 'TRUE' in
    multiple rhetorical phrases. Parser MUST NOT grab those."""
    text = (
        "## Verdict\n\n"
        "**FALSE** — Confidence: **HIGH**\n\n"
        "The claim CANNOT BE TRUE under the current code. "
        "Saying it is TRUE would require accepting a TRUE assumption "
        "that doesn't hold. TRUE TRUE TRUE.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "FALSE", (
        f"regression: parser grabbed 'TRUE' from rhetorical text instead of "
        f"the **FALSE** lead-line, got {verdict}"
    )


def test_does_not_pick_up_rhetorical_false_in_true_body() -> None:
    """Mirror: TRUE verdict body that says 'the hypothesis is FALSE'
    rhetorically (meaning "the protection-invariant the hyp claims
    doesn't hold = bug exists"). Confused parser before, must not now.

    Real APT38-treasury-drain case:
      `## Verdict\\n**TRUE** — Confidence: **HIGH**\\n\\nThe hypothesis
       that "every treasury-withdrawal function checks admin auth..."
       is **FALSE** — a critical real finding is confirmed.`
    """
    text = (
        "## Verdict\n\n"
        "**TRUE** — Confidence: **HIGH**\n\n"
        "The hypothesis that 'every treasury function checks auth' "
        "is **FALSE** — a critical real finding is confirmed at "
        "treasury.move:61.\n"
    )
    verdict, confidence = _parse_verdict(text)
    assert verdict == "TRUE"
    assert confidence == "HIGH"


def test_takes_last_verdict_section_when_multiple() -> None:
    """Some responses have a draft `## Verdict` section earlier (with
    a tentative TRUE) and a final `## Verdict` at the end with the
    revised FALSE. Parser must always take the LAST one."""
    text = (
        "## Verdict (draft, working through it)\n\n"
        "**TRUE** — leaning toward, need more checks\n\n"
        "## More analysis\n\n...\n\n"
        "## Verdict\n\n"
        "**FALSE** — Confidence: **HIGH**\n\n"
        "On full inspection, the precondition doesn't apply.\n"
    )
    verdict, _ = _parse_verdict(text)
    assert verdict == "FALSE"


def test_needs_layer_2_pattern() -> None:
    text = (
        "## Verdict\n\n"
        "NEEDS_LAYER_2 — Confidence: HIGH\n\n"
        "Empirical proof required.\n"
    )
    verdict, _ = _parse_verdict(text)
    assert verdict == "NEEDS_LAYER_2_TO_DECIDE"
