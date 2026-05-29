//! jelleo-attestation — on-chain attestation registry (P4).
//!
//! Conforms to the published schema in
//! audit-pipeline-cli/docs/methodology/07-attestation.md (live at
//! jelleo.com/methodology#attestation). Each cycle's signed Merkle root is
//! recorded on-chain, indexed by (protocol, cycle_id), so insurers / partner
//! programs can CPI-check that a fresh, signed attestation from the expected
//! key exists.
//!
//! On-chain `merkle_root` == the 32 bytes of the off-chain signed `merkle.json`
//! root (publisher reads the SIGNED sidecar, verifies the .sig, hex-decodes the
//! root, never recomputes — enforced in the P4.3 publisher).
//!
//! Security posture (reviewed by paranoid-goober + threat-modeler 2026-05-28):
//!   - `initialize` is UNFRONT-RUNNABLE: the authority is forced to the
//!     hardcoded EXPECTED_AUTHORITY regardless of who calls it, so a mempool
//!     front-run cannot install an attacker key (CRITICAL fix).
//!   - key rotation via two-step set_authority/accept_authority (HIGH fix) —
//!     a compromised key can be rotated without redeploy; the new key must
//!     sign accept, so a typo'd/uncontrolled target can't take over.
//!   - `slot` recorded alongside `ts` so consumers can enforce "fresh within N
//!     slots" per the doc (HIGH fix).
//!   - publish gated to `config.authority`; per-cycle account plain `init`
//!     (append-only immutability); NO `init_if_needed` (SOL-010); canonical
//!     bumps only (SOL-016). `protocol` is fixed-width 32B so the cycle PDA
//!     seed has no variable-seed collision surface.

use anchor_lang::prelude::*;

declare_id!("72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk");

/// The ONLY key allowed to be the initial authority. Forced in `initialize`
/// so the registry cannot be front-run at bootstrap. Rotate on-chain via
/// set_authority/accept_authority after deploy. Secret is OPERATOR-HELD off-repo
/// (generated 2026-05-28 to `~/.audit-keys/jelleo-attestation-authority.json`).
/// Replaced a prior stray auto-generated key (91ZW…) retired before any deploy.
const EXPECTED_AUTHORITY: Pubkey = pubkey!("CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL");

const MAX_CYCLE_ID_LEN: usize = 32; // "YYYYMMDD-HHMMSS" is 15; headroom
const MAX_ENGINE_SHA_LEN: usize = 64; // git SHA-1 hex is 40; headroom

#[program]
pub mod jelleo_attestation {
    use super::*;

    /// One-time global. Authority is FORCED to EXPECTED_AUTHORITY — caller
    /// identity is irrelevant, so a front-run cannot install a foreign key.
    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        let cfg = &mut ctx.accounts.config;
        cfg.authority = EXPECTED_AUTHORITY;
        cfg.pending_authority = None;
        cfg.bump = ctx.bumps.config;
        Ok(())
    }

    /// Step 1 of rotation: current authority nominates a new authority.
    /// (NEW-2) reject the system/default pubkey — accepting an unowned key would
    /// brick the registry. (NEW-4) refuse to overwrite an in-flight nomination;
    /// the operator must `cancel_rotation` first → no racy/ambiguous pending state.
    pub fn set_authority(ctx: Context<SetAuthority>, new_authority: Pubkey) -> Result<()> {
        let cfg = &mut ctx.accounts.config;
        require_keys_neq!(new_authority, Pubkey::default(), AttErr::InvalidAuthority);
        require_keys_neq!(new_authority, cfg.authority, AttErr::InvalidAuthority);
        require!(cfg.pending_authority.is_none(), AttErr::RotationPending);
        cfg.pending_authority = Some(new_authority);
        // Record the CURRENT authority at nomination time (threat-modeler) so the
        // audit trail can distinguish an operator typo from an attacker who
        // initiated a rotation with a compromised key — the cancelled/accepted
        // events alone can't show who started it.
        emit!(RotationInitiated {
            new_authority,
            current_authority: cfg.authority,
            ts: Clock::get()?.unix_timestamp,
        });
        Ok(())
    }

    /// Escape hatch (NEW-2/NEW-4): current authority clears an in-flight
    /// nomination (e.g. typo'd target) before it is accepted. Uses a DEDICATED
    /// `CancelRotation` accounts struct (not `SetAuthority`) so a future field
    /// added to SetAuthority can't silently change the cancel path's account
    /// requirements during an emergency rotation (paranoid-goober R1 #4).
    pub fn cancel_rotation(ctx: Context<CancelRotation>) -> Result<()> {
        let cfg = &mut ctx.accounts.config;
        // Idempotency guard (3-reviewer consensus): refuse to "cancel" when no
        // rotation is in flight — otherwise a spurious RotationCancelled event
        // (with no matching RotationInitiated) pollutes off-chain monitors.
        let cancelled = cfg.pending_authority.ok_or(AttErr::NoPendingAuthority)?;
        let cancelling_authority = cfg.authority;
        cfg.pending_authority = None;
        // Record WHICH key was cancelled AND who cancelled it (threat-modeler) so
        // the audit trail is complete without correlating against RotationInitiated.
        emit!(RotationCancelled {
            cancelled_authority: cancelled,
            cancelling_authority,
            ts: Clock::get()?.unix_timestamp,
        });
        Ok(())
    }

    /// Step 2 of rotation: the nominated key must itself sign to accept.
    pub fn accept_authority(ctx: Context<AcceptAuthority>) -> Result<()> {
        let cfg = &mut ctx.accounts.config;
        let pending = cfg.pending_authority.ok_or(AttErr::NoPendingAuthority)?;
        require_keys_eq!(ctx.accounts.new_authority.key(), pending, AttErr::Unauthorized);
        // Capture the outgoing authority BEFORE overwriting it, so AuthorityAccepted
        // records who handed off (threat-modeler: symmetric with RotationInitiated's
        // current_authority — completes the rotation audit trail).
        let old_authority = cfg.authority;
        cfg.authority = pending;
        cfg.pending_authority = None;
        emit!(AuthorityAccepted { new_authority: pending, old_authority, ts: Clock::get()?.unix_timestamp });
        Ok(())
    }

    /// Once per audited protocol: create its `latest` freshness pointer.
    /// Plain `init` (not init_if_needed) — fails if already registered.
    pub fn register_protocol(ctx: Context<RegisterProtocol>, protocol: Pubkey) -> Result<()> {
        let latest = &mut ctx.accounts.latest;
        latest.protocol = protocol;
        latest.latest_cycle_id = String::new();
        latest.latest_attestation = Pubkey::default();
        latest.ts = 0;
        latest.slot = 0;
        latest.bump = ctx.bumps.latest;
        Ok(())
    }

    /// Record one cycle's attestation. Gated to `config.authority`.
    /// Per-cycle account is immutable (plain init). Updates the existing
    /// `latest` pointer (must be registered first).
    pub fn publish_attestation(
        ctx: Context<PublishAttestation>,
        protocol: Pubkey,
        cycle_id: String,
        engine_sha: String,
        invariant_count: u32,
        merkle_root: [u8; 32],
    ) -> Result<()> {
        require!(cycle_id.len() <= MAX_CYCLE_ID_LEN, AttErr::CycleIdTooLong);
        require!(engine_sha.len() <= MAX_ENGINE_SHA_LEN, AttErr::EngineShaTooLong);

        let clock = Clock::get()?;
        let ts = clock.unix_timestamp;
        let slot = clock.slot;

        let att = &mut ctx.accounts.cycle_attestation;
        att.protocol = protocol;
        att.cycle_id = cycle_id.clone();
        att.engine_sha = engine_sha;
        att.invariant_count = invariant_count;
        att.merkle_root = merkle_root;
        att.signer = ctx.accounts.authority.key();
        att.ts = ts;
        att.slot = slot;
        att.bump = ctx.bumps.cycle_attestation;

        let latest = &mut ctx.accounts.latest;
        latest.latest_cycle_id = cycle_id;
        latest.latest_attestation = att.key();
        latest.ts = ts;
        latest.slot = slot;

        emit!(AttestationPublished { protocol, cycle_id: att.cycle_id.clone(), merkle_root, ts, slot });
        Ok(())
    }
}

#[account]
pub struct Config {
    pub authority: Pubkey,
    pub pending_authority: Option<Pubkey>,
    pub bump: u8,
}
impl Config {
    pub const SIZE: usize = 8 + 32 + (1 + 32) + 1;
}

/// Mirrors docs/methodology/07-attestation.md account layout (+ slot for freshness).
#[account]
pub struct CycleAttestation {
    pub protocol: Pubkey,        // audited protocol's program id (fixed 32B)
    pub cycle_id: String,        // "YYYYMMDD-HHMMSS"
    pub engine_sha: String,      // engine git SHA (hex)
    pub invariant_count: u32,    // = n_findings / n_leaves from merkle.json
    pub merkle_root: [u8; 32],   // == off-chain signed merkle.json root bytes
    pub signer: Pubkey,          // publishing platform key (== config.authority)
    pub ts: i64,                 // Clock unix ts
    pub slot: u64,               // Clock slot — consumers enforce N-slot freshness
    pub bump: u8,
}
impl CycleAttestation {
    pub const SIZE: usize =
        8 + 32 + (4 + MAX_CYCLE_ID_LEN) + (4 + MAX_ENGINE_SHA_LEN) + 4 + 32 + 32 + 8 + 8 + 1;
}

/// Freshness pointer: `cycle_attestation_<protocol>_latest` in the doc.
#[account]
pub struct Latest {
    pub protocol: Pubkey,
    pub latest_cycle_id: String,
    pub latest_attestation: Pubkey,
    pub ts: i64,
    pub slot: u64,
    pub bump: u8,
}
impl Latest {
    pub const SIZE: usize = 8 + 32 + (4 + MAX_CYCLE_ID_LEN) + 32 + 8 + 8 + 1;
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(init, payer = payer, space = Config::SIZE, seeds = [b"config"], bump)]
    pub config: Account<'info, Config>,
    #[account(mut)]
    pub payer: Signer<'info>,
    pub system_program: Program<'info, System>,
}

/// INVARIANT (paranoid-goober/threat-modeler): this struct's shape is NOT
/// automatically inherited by `CancelRotation` — they are intentionally
/// decoupled. If you add/loosen an account here, evaluate `CancelRotation`
/// SEPARATELY; both are authorization boundaries (current authority must sign).
#[derive(Accounts)]
pub struct SetAuthority<'info> {
    #[account(mut, seeds = [b"config"], bump = config.bump, has_one = authority @ AttErr::Unauthorized)]
    pub config: Account<'info, Config>,
    pub authority: Signer<'info>,
}

/// Dedicated accounts for `cancel_rotation` — intentionally a SEPARATE struct
/// from `SetAuthority` (paranoid-goober R1 #4) so future changes to SetAuthority
/// can't silently alter the emergency-cancel path. Same shape today: the current
/// authority must sign (`has_one = authority`); config is the canonical PDA.
#[derive(Accounts)]
pub struct CancelRotation<'info> {
    #[account(mut, seeds = [b"config"], bump = config.bump, has_one = authority @ AttErr::Unauthorized)]
    pub config: Account<'info, Config>,
    pub authority: Signer<'info>,
}

#[derive(Accounts)]
pub struct AcceptAuthority<'info> {
    // Framework-level defense-in-depth (threat-modeler): bind the signer to the
    // pending nomination at the ACCOUNT layer, not only in the body. `has_one`
    // can't target an Option<Pubkey>, so an explicit constraint is used. If
    // pending is None, `None == Some(..)` is false → Unauthorized. This makes
    // the takeover-prevention property hold even if the body check is ever
    // refactored away.
    #[account(
        mut,
        seeds = [b"config"],
        bump = config.bump,
        constraint = config.pending_authority == Some(new_authority.key()) @ AttErr::Unauthorized
    )]
    pub config: Account<'info, Config>,
    pub new_authority: Signer<'info>,
}

#[derive(Accounts)]
#[instruction(protocol: Pubkey)]
pub struct RegisterProtocol<'info> {
    #[account(seeds = [b"config"], bump = config.bump, has_one = authority @ AttErr::Unauthorized)]
    pub config: Account<'info, Config>,
    #[account(
        init,
        payer = authority,
        space = Latest::SIZE,
        seeds = [b"latest", protocol.as_ref()],
        bump
    )]
    pub latest: Account<'info, Latest>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(protocol: Pubkey, cycle_id: String)]
pub struct PublishAttestation<'info> {
    #[account(seeds = [b"config"], bump = config.bump, has_one = authority @ AttErr::Unauthorized)]
    pub config: Account<'info, Config>,
    #[account(
        init,
        payer = authority,
        space = CycleAttestation::SIZE,
        seeds = [b"cycle", protocol.as_ref(), cycle_id.as_bytes()],
        bump
    )]
    pub cycle_attestation: Account<'info, CycleAttestation>,
    #[account(
        mut,
        seeds = [b"latest", protocol.as_ref()],
        bump = latest.bump,
        constraint = latest.protocol == protocol @ AttErr::ProtocolMismatch
    )]
    pub latest: Account<'info, Latest>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[event]
pub struct AttestationPublished {
    pub protocol: Pubkey,
    pub cycle_id: String,
    pub merkle_root: [u8; 32],
    pub ts: i64,
    pub slot: u64,
}

// Rotation lifecycle events (NEW-5) — let off-chain monitors / auditors observe
// authority changes without polling Config.
#[event]
pub struct RotationInitiated {
    pub new_authority: Pubkey,
    pub current_authority: Pubkey,
    pub ts: i64,
}

#[event]
pub struct RotationCancelled {
    pub cancelled_authority: Pubkey,
    pub cancelling_authority: Pubkey,
    pub ts: i64,
}

#[event]
pub struct AuthorityAccepted {
    pub new_authority: Pubkey,
    pub old_authority: Pubkey,
    pub ts: i64,
}

#[error_code]
pub enum AttErr {
    #[msg("cycle_id exceeds max length")]
    CycleIdTooLong,
    #[msg("engine_sha exceeds max length")]
    EngineShaTooLong,
    #[msg("signer is not the configured authority")]
    Unauthorized,
    #[msg("latest pointer protocol does not match")]
    ProtocolMismatch,
    #[msg("no pending authority to accept")]
    NoPendingAuthority,
    #[msg("new authority is invalid (default key or same as current)")]
    InvalidAuthority,
    #[msg("a rotation is already pending; cancel it first")]
    RotationPending,
}
