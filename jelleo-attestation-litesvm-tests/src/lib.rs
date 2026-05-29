//! Standalone litesvm harness for the jelleo-attestation program (P4.2).
//!
//! Loads the prebuilt BPF `.so` and drives it with manually-encoded Anchor
//! instructions (8-byte `sha256("global:<ix>")` discriminator + borsh args), so
//! the harness has NO anchor-lang dependency / version coupling to the program.
//! Where a test must act AS the registry authority, it crafts the Config PDA
//! directly via `svm.set_account` with a test-controlled key — the real
//! hardcoded EXPECTED_AUTHORITY private key is never needed or handled.
//!
//! Runs on Linux (openssl-dev present). Windows can't compile litesvm
//! (solana-secp256r1-program -> openssl, no MSVC openssl/nasm) — see the P4.2
//! task notes.

#[cfg(test)]
mod tests {
    use litesvm::LiteSVM;
    use sha2::{Digest, Sha256};
    use solana_account::Account;
    use solana_instruction::{AccountMeta, Instruction};
    use solana_keypair::Keypair;
    use solana_pubkey::Pubkey;
    use solana_signer::Signer;
    use solana_transaction::Transaction;
    use std::str::FromStr;

    const PROGRAM_ID: &str = "72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk";
    const EXPECTED_AUTHORITY: &str = "CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL";
    const SO_PATH: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../jelleo-attestation/target/deploy/jelleo_attestation.so"
    );
    const CONFIG_SIZE: usize = 8 + 32 + (1 + 32) + 1; // = 74

    fn program_id() -> Pubkey {
        Pubkey::from_str(PROGRAM_ID).unwrap()
    }
    // System Program ID == the all-zero pubkey (SOL-018: typed, no string literal).
    fn system_program() -> Pubkey {
        Pubkey::default()
    }
    fn unique() -> Pubkey {
        Keypair::new().pubkey()
    }

    fn disc(kind: &str, name: &str) -> [u8; 8] {
        let mut h = Sha256::new();
        h.update(format!("{kind}:{name}").as_bytes());
        let r = h.finalize();
        let mut d = [0u8; 8];
        d.copy_from_slice(&r[..8]);
        d
    }
    fn ix_disc(name: &str) -> [u8; 8] {
        disc("global", name)
    }

    fn setup() -> LiteSVM {
        let mut svm = LiteSVM::new();
        let so = std::fs::read(SO_PATH).expect("read jelleo_attestation.so (cargo build-sbf)");
        svm.add_program(program_id(), &so);
        svm
    }

    fn config_pda() -> (Pubkey, u8) {
        Pubkey::find_program_address(&[b"config"], &program_id())
    }
    fn latest_pda(protocol: &Pubkey) -> (Pubkey, u8) {
        Pubkey::find_program_address(&[b"latest", protocol.as_ref()], &program_id())
    }

    fn funded(svm: &mut LiteSVM) -> Keypair {
        let kp = Keypair::new();
        svm.airdrop(&kp.pubkey(), 10_000_000_000).unwrap();
        kp
    }

    fn send(svm: &mut LiteSVM, ixs: &[Instruction], payer: &Keypair, signers: &[&Keypair]) -> Result<(), String> {
        let tx = Transaction::new_signed_with_payer(ixs, Some(&payer.pubkey()), signers, svm.latest_blockhash());
        svm.send_transaction(tx).map(|_| ()).map_err(|e| format!("{e:?}"))
    }

    /// Craft the Config PDA directly with a chosen authority + pending. Lays out
    /// bytes exactly as anchor's borsh: disc | authority | Option<Pubkey> | bump.
    /// The stored bump must equal the canonical PDA bump so the program's
    /// `seeds+bump` check passes.
    fn craft_config(svm: &mut LiteSVM, authority: &Pubkey, pending: Option<Pubkey>) {
        let (config, bump) = config_pda();
        let mut data = vec![0u8; CONFIG_SIZE];
        data[0..8].copy_from_slice(&disc("account", "Config"));
        data[8..40].copy_from_slice(authority.as_ref());
        match pending {
            None => {
                data[40] = 0; // None tag
                data[41] = bump; // bump right after the 1-byte None tag
            }
            Some(p) => {
                data[40] = 1; // Some tag
                data[41..73].copy_from_slice(p.as_ref());
                data[73] = bump; // bump after tag + pubkey
            }
        }
        svm.set_account(
            config,
            Account { lamports: 10_000_000, data, owner: program_id(), executable: false, rent_epoch: 0 },
        )
        .unwrap();
    }

    fn read_config(svm: &LiteSVM) -> (Pubkey, Option<Pubkey>) {
        let acct = svm.get_account(&config_pda().0).expect("config exists");
        let authority = Pubkey::try_from(&acct.data[8..40]).unwrap();
        let pending = if acct.data[40] == 1 {
            Some(Pubkey::try_from(&acct.data[41..73]).unwrap())
        } else {
            None
        };
        (authority, pending)
    }

    // ---- instruction builders ----
    fn initialize_ix(payer: &Pubkey) -> Instruction {
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new(config_pda().0, false),
                AccountMeta::new(*payer, true),
                AccountMeta::new_readonly(system_program(), false),
            ],
            data: ix_disc("initialize").to_vec(),
        }
    }
    fn set_authority_ix(authority: &Pubkey, new_authority: &Pubkey) -> Instruction {
        let mut data = ix_disc("set_authority").to_vec();
        data.extend_from_slice(new_authority.as_ref());
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new(config_pda().0, false),
                AccountMeta::new_readonly(*authority, true),
            ],
            data,
        }
    }
    fn accept_authority_ix(new_authority: &Pubkey) -> Instruction {
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new(config_pda().0, false),
                AccountMeta::new_readonly(*new_authority, true),
            ],
            data: ix_disc("accept_authority").to_vec(),
        }
    }
    fn cancel_rotation_ix(authority: &Pubkey) -> Instruction {
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new(config_pda().0, false),
                AccountMeta::new_readonly(*authority, true),
            ],
            data: ix_disc("cancel_rotation").to_vec(),
        }
    }
    fn register_protocol_ix(authority: &Pubkey, protocol: &Pubkey) -> Instruction {
        let mut data = ix_disc("register_protocol").to_vec();
        data.extend_from_slice(protocol.as_ref());
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new_readonly(config_pda().0, false),
                AccountMeta::new(latest_pda(protocol).0, false),
                AccountMeta::new(*authority, true),
                AccountMeta::new_readonly(system_program(), false),
            ],
            data,
        }
    }

    fn cycle_pda(protocol: &Pubkey, cycle_id: &str) -> (Pubkey, u8) {
        Pubkey::find_program_address(
            &[b"cycle", protocol.as_ref(), cycle_id.as_bytes()],
            &program_id(),
        )
    }

    /// Borsh-encode a String: 4-byte LE length prefix + UTF-8 bytes.
    /// (Rust `str::len()` IS the UTF-8 byte count — intentional, matches borsh.)
    fn borsh_string(s: &str) -> Vec<u8> {
        let mut v = (s.len() as u32).to_le_bytes().to_vec();
        v.extend_from_slice(s.as_bytes());
        v
    }

    #[allow(clippy::too_many_arguments)]
    fn publish_attestation_ix(
        authority: &Pubkey,
        protocol: &Pubkey,
        cycle_id: &str,
        engine_sha: &str,
        invariant_count: u32,
        merkle_root: [u8; 32],
    ) -> Instruction {
        let mut data = ix_disc("publish_attestation").to_vec();
        data.extend_from_slice(protocol.as_ref());
        data.extend_from_slice(&borsh_string(cycle_id));
        data.extend_from_slice(&borsh_string(engine_sha));
        data.extend_from_slice(&invariant_count.to_le_bytes());
        data.extend_from_slice(&merkle_root);
        // Account order MUST match PublishAttestation<'info>:
        // config(ro) | cycle_attestation(mut) | latest(mut) | authority(signer,mut) | system(ro)
        Instruction {
            program_id: program_id(),
            accounts: vec![
                AccountMeta::new_readonly(config_pda().0, false),
                AccountMeta::new(cycle_pda(protocol, cycle_id).0, false),
                AccountMeta::new(latest_pda(protocol).0, false),
                AccountMeta::new(*authority, true),
                AccountMeta::new_readonly(system_program(), false),
            ],
            data,
        }
    }

    /// Read `latest.latest_cycle_id`. Layout: disc(8) | protocol(32) | string(4+len) | ...
    fn read_latest_cycle_id(svm: &LiteSVM, protocol: &Pubkey) -> String {
        let acct = svm.get_account(&latest_pda(protocol).0).expect("latest exists");
        let len = u32::from_le_bytes(acct.data[40..44].try_into().unwrap()) as usize;
        String::from_utf8(acct.data[44..44 + len].to_vec()).unwrap()
    }

    // ============================ TESTS ============================

    #[test]
    fn initialize_forces_expected_authority_unfront_runnable() {
        let mut svm = setup();
        let attacker = funded(&mut svm); // random funded key — a front-runner
        send(&mut svm, &[initialize_ix(&attacker.pubkey())], &attacker, &[&attacker])
            .expect("initialize should succeed for any payer");
        let (authority, _) = read_config(&svm);
        assert_eq!(
            authority,
            Pubkey::from_str(EXPECTED_AUTHORITY).unwrap(),
            "front-run resistance: authority forced to EXPECTED_AUTHORITY regardless of payer"
        );
    }

    #[test]
    fn initialize_twice_fails() {
        let mut svm = setup();
        let p = funded(&mut svm);
        send(&mut svm, &[initialize_ix(&p.pubkey())], &p, &[&p]).expect("first init ok");
        let p2 = funded(&mut svm);
        assert!(
            send(&mut svm, &[initialize_ix(&p2.pubkey())], &p2, &[&p2]).is_err(),
            "second initialize must fail — config is created once (plain init)"
        );
    }

    #[test]
    fn register_protocol_requires_authority() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        // The authority can register (also validates craft_config + has_one path).
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("authority register_protocol should succeed");
        // A non-authority signer cannot.
        let rando = funded(&mut svm);
        let protocol2 = unique();
        assert!(
            send(&mut svm, &[register_protocol_ix(&rando.pubkey(), &protocol2)], &rando, &[&rando]).is_err(),
            "non-authority register_protocol must fail (has_one Unauthorized)"
        );
    }

    #[test]
    fn accept_authority_rejects_when_no_pending() {
        // The new AcceptAuthority account-level constraint (threat-modeler HIGH):
        // with no pending nomination, accept must be rejected at the framework layer.
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let rando = funded(&mut svm);
        assert!(
            send(&mut svm, &[accept_authority_ix(&rando.pubkey())], &rando, &[&rando]).is_err(),
            "accept_authority with no pending must fail (AcceptAuthority constraint)"
        );
    }

    #[test]
    fn cancel_rotation_rejects_when_no_pending() {
        // The idempotency guard (3-reviewer consensus): no phantom cancels.
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        assert!(
            send(&mut svm, &[cancel_rotation_ix(&auth.pubkey())], &auth, &[&auth]).is_err(),
            "cancel_rotation with no pending must fail (idempotency guard)"
        );
    }

    #[test]
    fn rotation_two_step_happy_path() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let new_auth = funded(&mut svm);
        // Step 1: current authority nominates new_auth.
        send(&mut svm, &[set_authority_ix(&auth.pubkey(), &new_auth.pubkey())], &auth, &[&auth])
            .expect("set_authority should succeed");
        let (_, pending) = read_config(&svm);
        assert_eq!(pending, Some(new_auth.pubkey()), "pending == nominated key");
        // Step 2: the nominated key itself accepts.
        send(&mut svm, &[accept_authority_ix(&new_auth.pubkey())], &new_auth, &[&new_auth])
            .expect("accept_authority should succeed");
        let (authority, pending) = read_config(&svm);
        assert_eq!(authority, new_auth.pubkey(), "authority rotated to nominee");
        assert_eq!(pending, None, "pending cleared after accept");
    }

    #[test]
    fn cancel_rotation_clears_pending() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        let nominee = unique();
        craft_config(&mut svm, &auth.pubkey(), Some(nominee));
        send(&mut svm, &[cancel_rotation_ix(&auth.pubkey())], &auth, &[&auth])
            .expect("cancel_rotation with a pending nomination should succeed");
        let (_, pending) = read_config(&svm);
        assert_eq!(pending, None, "pending cleared after cancel");
    }

    // ---- publish_attestation (P4 hardening: was previously untested) ----

    /// Authority publishes a cycle: the per-cycle PDA is created (program-owned)
    /// and the `latest` freshness pointer advances to that cycle_id.
    #[test]
    fn publish_attestation_happy_path() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("register_protocol");
        let cid = "20260101-000000";
        send(
            &mut svm,
            &[publish_attestation_ix(&auth.pubkey(), &protocol, cid, "abc1234", 7, [0xAB; 32])],
            &auth,
            &[&auth],
        )
        .expect("authority publish should succeed");
        let cyc = svm.get_account(&cycle_pda(&protocol, cid).0).expect("cycle attestation exists");
        assert_eq!(cyc.owner, program_id(), "cycle PDA owned by the program");
        assert_eq!(read_latest_cycle_id(&svm, &protocol), cid, "latest pointer advanced to this cycle");
    }

    /// A non-authority signer cannot publish (has_one = authority @ Unauthorized).
    #[test]
    fn publish_attestation_rejects_non_authority() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("register_protocol");
        let rando = funded(&mut svm);
        assert!(
            send(
                &mut svm,
                &[publish_attestation_ix(&rando.pubkey(), &protocol, "20260101-000000", "abc1234", 1, [0u8; 32])],
                &rando,
                &[&rando],
            )
            .is_err(),
            "non-authority publish must fail (has_one Unauthorized)"
        );
    }

    /// Re-publishing the same (protocol, cycle_id) fails — the per-cycle account is
    /// plain `init`, so the registry is append-only / immutable per cycle.
    #[test]
    fn publish_attestation_duplicate_cycle_fails() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("register_protocol");
        let cid = "20260101-000000";
        send(&mut svm, &[publish_attestation_ix(&auth.pubkey(), &protocol, cid, "abc1234", 1, [1u8; 32])], &auth, &[&auth])
            .expect("first publish ok");
        assert!(
            send(&mut svm, &[publish_attestation_ix(&auth.pubkey(), &protocol, cid, "abc1234", 1, [2u8; 32])], &auth, &[&auth]).is_err(),
            "duplicate (protocol, cycle_id) must fail — per-cycle account is plain init"
        );
    }

    /// NEW guard: empty cycle_id is rejected on-chain (CycleIdEmpty).
    #[test]
    fn publish_attestation_rejects_empty_cycle_id() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("register_protocol");
        assert!(
            send(&mut svm, &[publish_attestation_ix(&auth.pubkey(), &protocol, "", "abc1234", 1, [0u8; 32])], &auth, &[&auth]).is_err(),
            "empty cycle_id must be rejected (CycleIdEmpty guard)"
        );
    }

    /// NEW guard: empty engine_sha is rejected on-chain (EngineShaEmpty).
    #[test]
    fn publish_attestation_rejects_empty_engine_sha() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique();
        send(&mut svm, &[register_protocol_ix(&auth.pubkey(), &protocol)], &auth, &[&auth])
            .expect("register_protocol");
        assert!(
            send(&mut svm, &[publish_attestation_ix(&auth.pubkey(), &protocol, "20260101-000000", "", 1, [0u8; 32])], &auth, &[&auth]).is_err(),
            "empty engine_sha must be rejected (EngineShaEmpty guard)"
        );
    }

    /// Publishing for a protocol that was never `register_protocol`'d fails —
    /// the `latest` PDA does not exist, so the mut-account load fails.
    #[test]
    fn publish_attestation_unregistered_protocol_fails() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let protocol = unique(); // NOT registered
        assert!(
            send(&mut svm, &[publish_attestation_ix(&auth.pubkey(), &protocol, "20260101-000000", "abc1234", 1, [0u8; 32])], &auth, &[&auth]).is_err(),
            "publish for an unregistered protocol must fail (latest PDA missing)"
        );
    }

    /// A non-authority cannot nominate a new authority (set_authority has_one).
    #[test]
    fn set_authority_rejects_non_authority() {
        let mut svm = setup();
        let auth = funded(&mut svm);
        craft_config(&mut svm, &auth.pubkey(), None);
        let rando = funded(&mut svm);
        assert!(
            send(&mut svm, &[set_authority_ix(&rando.pubkey(), &unique())], &rando, &[&rando]).is_err(),
            "non-authority set_authority must fail (has_one Unauthorized)"
        );
    }
}
