# Reusability checklist

Before applying this pipeline to a new Solana program, verify these prerequisites. Items marked **REQUIRED** will cause the pipeline to not work or to produce misleading results if missing. Items marked **RECOMMENDED** are workarounds-available but the audit will be slower or weaker without them.

## About the target program

| Item | Status |
|---|---|
| Engine library + BPF wrapper architecture | **REQUIRED** — pipeline assumes this split. If the target is monolithic (no engine/wrapper split), Layers 3+4 still work but you do them on the same crate. |
| Source code is publicly available at a pinned SHA | **REQUIRED** — you need to be able to reference exact line numbers in disclosures. |
| Written in Rust | **REQUIRED** for Layer 3 (Kani is Rust-only). For C/C++ Solana programs (rare), substitute SeaHorn or similar. |
| Has an English-language spec, design doc, or thorough comments | **RECOMMENDED** — Layer 1's hypothesis generation comes from spec-vs-code gap analysis. Without a spec, you're inferring intent from code, which is slower. |
| Has existing Kani harnesses (any number) | **RECOMMENDED** — re-running the existing baseline is a strong Layer 5 signal. If the target has none, you can still write yours; you just don't get the "no regressions" claim. |
| Engine state is accessible via test-visible methods | **RECOMMENDED** — if the engine hides state behind opaque getters, your Layer 2 PoCs will be harder to write. Check for `test_visible!` macros or `#[cfg(feature = "test")]` exposing fields. |
| Builds cleanly with `cargo build` | **REQUIRED** |
| Has a test suite that runs (any size) | **REQUIRED** — confirms the toolchain works. |

## About the maintainer

| Item | Status |
|---|---|
| Active development (commits in last 60 days) | **RECOMMENDED** — if the maintainer is dormant, your disclosure may sit unactioned. |
| Open to security disclosures | **RECOMMENDED** — check their README, security policy, prior disclosure handling on GitHub issues. |
| Reachable via GitHub or other public channel | **REQUIRED** — disclosure has to land somewhere. |

## Your environment

| Item | Status |
|---|---|
| Rust 1.95+ installed locally | **REQUIRED** |
| Solana CLI 3.1+ + `cargo build-sbf` | **REQUIRED** for Layer 4 (LiteSVM) |
| Kani 0.67+ installed locally | **REQUIRED** for Layer 3, OR remote on the VPS |
| Dedicated VPS with audit toolchain | **REQUIRED** for Layer 5; **RECOMMENDED** for heavy Layer 3 runs |
| SSH key for VPS access | **REQUIRED** if using VPS |
| `gh` CLI authenticated | **REQUIRED** for filing issues / PRs |
| Multi-agent orchestration capability | **REQUIRED** for Layer 1 (Claude with subagent dispatch, or equivalent) |

## Engine constants you need to enumerate

The pipeline's hypothesis-generation step needs these constants for the target. Enumerate them BEFORE Layer 1:

```
- MAX_ACCOUNTS                  (per-market account cap)
- MAX_<asset>_TVL              (total value locked cap)
- MAX_<account>_POSITIVE_PNL   (per-account upper bound on PnL)
- MAX_<position>_ABS_Q         (position size cap)
- Per-slot rate-of-change caps (max_price_move_bps_per_slot, etc.)
- Margin parameters            (initial_margin_bps, maintenance_margin_bps)
- Insurance parameters         (insurance fund size, withdrawal caps, cooldowns)
- Time horizons                (h_min, h_max, max_accrual_dt_slots, etc.)
```

Without these, you can't write meaningful Kani harnesses or LiteSVM bound analyses — you don't know what state space to symbolically explore.

## BPF instructions you need to enumerate

For each public BPF instruction the wrapper exposes:

```
Instruction:    <name>
Permission:     permissionless / signer-required / admin-required
Reaches engine: <list of engine functions called>
State writes:   <list of engine state fields mutated>
```

This is the input to Layer 4 (reachability analysis). Without it, you don't know which findings need a LiteSVM exploit chain vs which are admin-gated.

## Time investment estimate

If all REQUIRED items are present and most RECOMMENDED items are too:
- First-time audit (toolchain setup + initial enumeration + 1-3 findings): **2-3 weeks**
- Repeat audit on similar codebase: **1 week**
- Single-finding follow-up (e.g., bounty hunt): **1-3 days**

If REQUIRED items are missing:
- Add 1-3 days to set up substitute infrastructure (e.g., adapt to a non-Rust target)
- May need to skip Layer 3 entirely if Kani can't be applied

## When the pipeline does NOT fit

| Scenario | Recommendation |
|---|---|
| Target is closed-source | Pipeline doesn't apply; you'd need binary analysis tools (Ghidra, etc.) |
| Target is in C / C++ | Substitute SeaHorn for Layer 3; rest of pipeline carries over |
| Target is in non-Solana ecosystem (Ethereum, Cosmos, etc.) | Layers 1, 2, 3 carry over; Layer 4 needs the equivalent VM (Foundry for EVM, etc.); Layer 5 same |
| Target is a frontend / off-chain service | Pipeline doesn't fit; this is a security audit pipeline for protocol-level state machines |
| Target is "simple" — small codebase, few public instructions | Layers 1+2 may be enough; Layers 3+4 add overhead without commensurate value |

## Final pre-flight check

Before starting the audit, satisfy this checklist:

- [ ] Target code cloned at a pinned SHA, builds cleanly
- [ ] Engine constants enumerated (cap list above)
- [ ] BPF instruction surface enumerated
- [ ] VPS provisioned and SSH key working
- [ ] Toolchain installed both locally and on VPS, versions match
- [ ] Maintainer's existing Kani baseline runs successfully (if any)
- [ ] Multi-agent dispatch capability tested with a trivial hypothesis
- [ ] Disclosure target identified (where will the issue / PR land?)

Once all checked, proceed to Layer 1 with the [agent prompts library](../agent_prompts/).
