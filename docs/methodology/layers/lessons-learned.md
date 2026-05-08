# Lessons learned (operational)

These are non-obvious gotchas accumulated while running the pipeline on Percolator. Read this BEFORE starting a new audit; it'll save you several hours.

## Cargo + Kani

### 1. Cargo compiles ALL tests when ANY test is requested

A broken file in `tests/` blocks unrelated harness runs. If you have `proofs_a.rs` (working) and `proofs_b.rs` (broken), running `cargo kani --harness proof_in_a` will FAIL because cargo tries to compile the entire test target including the broken `proofs_b.rs`.

**Always** `cargo check --tests --features test` before dispatching Kani. Confirms the test crate compiles before you spend hours waiting for Kani.

### 2. `RiskEngine::new` (or your equivalent constructor) runs an init loop under cfg(kani)

Most engine constructors initialize an array of accounts. Under `cfg(kani)`, this loop has a small bound (typically `MAX_ACCOUNTS - 1` iterations). Default `#[kani::unwind(2)]` is too low.

**Rule**: minimum unwind for any harness that constructs the engine is `MAX_ACCOUNTS_KANI + 2`. For Percolator (`MAX_ACCOUNTS = 4` under cfg kani), that's `unwind(10)` minimum. Use 10 unless you have a reason to bound tighter.

### 3. Kani's `unwinding assertion loop 0` failures are NOISE

If the failed check is `unwinding assertion loop 0` at `<builtin-library-memcmp>` or in synthesized init code, that's NOT a real bug. It means Kani gave up at the unwind boundary in setup.

Real findings show up as `expect_failed.assertion.1` at `option.rs:2184` (for `.expect()` panics) or as named `assert!` failures.

### 4. Background agents can hallucinate verdicts

Always cross-check claimed verdicts against raw artifacts. If an agent says "VERIFICATION FAILED with CEX," `tail -5` the actual log file before promoting it into a finding.

## LiteSVM

### 5. BPF artifact compatibility

The `.so` file built by `cargo build-sbf` must match the test's feature flags. Otherwise `init_market` fails with cryptic `Custom(4)` errors (slab size mismatch).

**Pattern**:
```bash
cargo build-sbf --features small  # build artifact with --features small
cargo test --features small        # run tests with --features small
```

If you mix and match, you'll waste 30 min debugging before realizing.

### 6. Slab-offset readers are BPF-target-specific

u128 aligns to 8 bytes on SBF, 16 on x86. Field offsets in your tests must be observed empirically against the compiled `.so`, not derived from `size_of` at native compile time.

When first writing a slab-offset reader, sanity-check with TWO tests:
1. Read a value at the offset
2. Mutate via a known instruction
3. Read again
4. Assert the change matches expectation

If step 4 fails, your offset is wrong.

## VPS

### 7. tmux is non-negotiable for Kani runs

A single Kani harness can take 10 minutes - 1 hour. SSH sessions disconnect; tmux survives.

Always:
```bash
ssh <host> "tmux new-session -d -s kani_<name> 'cargo kani --harness <name>'"
```

Don't run Kani in a foreground SSH session; you'll cry when your laptop sleeps.

### 8. Disk fills up faster than you think

`cargo kani` produces multi-GB intermediate files (`target/kani/`). After 30 harness runs, you've consumed ~50 GB.

**Pattern**: clean periodically:
```bash
ssh <host> "cd /tmp/audit/engine && cargo clean -p <crate> 2>/dev/null"
```

Or provision a 100 GB disk if you're running long audits.

### 9. Don't run multiple Kani sessions on the same crate concurrently

Kani writes to `target/kani/`. Two concurrent runs corrupt each other. Use separate working directories or serialize.

If you need parallelism, clone the engine into multiple working dirs:
```bash
cp -r /tmp/audit/engine /tmp/audit/engine_run2
```

## Multi-agent orchestration

### 10. Hypothesis bias

"Find this bug" produces false positives. Frame as "is this invariant true?" — produces clean negatives that strengthen the disclosure.

### 11. Agent over-confidence on file/line citations

Agents will sometimes cite `engine line 3010` when the actual function is at `engine line 3865`. The line numbers in their analysis may be relative to a different file structure they imagined.

Before promoting any agent-cited line number into a published disclosure, **grep the actual source for the function name and verify the line**.

### 12. Cross-check between agents

Spawn 2 agents on the same hypothesis with different framing. If they disagree, one (or both) is wrong. The disagreement IS the signal — investigate.

## Disclosure writing

### 13. Anti-self-praise vocabulary

Words to avoid in disclosure prose: "rigorous," "comprehensive," "thorough," "honestly," "carefully." They sound like you're insisting on something the evidence should show by itself.

### 14. Never use the disclosure to dunk

You're collaborating with the maintainer, not winning a debate. "We found X" — fine. "You missed X" — never.

### 15. Always include negative results

If you investigated 5 hypotheses and 2 turned out to not be bugs, disclose that. It builds trust that you're not inflating finding counts.

## Operational

### 16. Privatize old GitHub repos before major disclosure

Once a maintainer publicly tags your handle, hundreds of curious people will browse your profile. If you have unrelated old repos that don't reflect your current capability, make them private BEFORE the public moment.

### 17. Public attribution > DM follow-ups

If a maintainer publicly attributes work to you, that's their preferred form of acknowledgment. Don't DM them to confirm "did you see my tweet?" — that comes across as junior.

### 18. Issue body should be a HOOK, not the disclosure itself

Keep the issue body short. The substance lives in your linked research repo. The issue's job is to give the maintainer enough to know whether to click.

## Toolchain pinning

### 19. Engine SHA + wrapper SHA both matter

Solana programs typically split engine (Rust library) from wrapper (BPF entrypoints). Both repos have HEAD. Pin BOTH at the start of the audit:

```
Engine pin: aeyakovenko/percolator @ master sha 5940285
Wrapper pin: aeyakovenko/percolator-prog @ main sha c447686
```

If the maintainer updates one repo mid-audit, your line citations may drift. Re-anchor before publication.

### 20. Check upstream HEAD before publication

Right before filing the disclosure, run:

```bash
gh api repos/<org>/<engine-repo>/commits/master --jq .sha
gh api repos/<org>/<wrapper-repo>/commits/main --jq .sha
```

If they differ from your pin, decide whether to re-anchor or note the disclosure is against the pinned sha. Don't be surprised by deployed changes the day before your call.
