# Audit Finding: A1-permissionless-no-drain

## Investigation Summary

I will trace every permissionless BPF instruction to determine whether any can reduce vault balance below the safety invariant: `vault ≥ cash_locked + claimable_pnl + insurance.balance`.

---

## Step 1: Enumerate BPF Entry Points

Let me read the wrapper source to find all instruction handlers.

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/lib.rs"}}
</tool_call>
<tool_response>
use solana_program::{
    account_info::AccountInfo, entrypoint, entrypoint::ProgramResult, pubkey::Pubkey,
};

mod percolator;
pub use percolator::*;

entrypoint!(process_instruction);
fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    percolator::process_instruction(program_id, accounts, instruction_data)
}
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs"}}
</tool_call>
<tool_response>
(23155 lines)
...
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "fn process_instruction"}}
</tool_call>
<tool_response>
Line 23103: pub fn process_instruction(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 23103, "end_line": 23155}}
</tool_call>
<tool_response>
pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    if instruction_data.is_empty() {
        return Err(ProgramError::InvalidInstructionData);
    }
    let tag = instruction_data[0];
    match tag {
        0 => initialize(program_id, accounts, &instruction_data[1..]),
        1 => deposit(program_id, accounts, &instruction_data[1..]),
        2 => withdraw(program_id, accounts, &instruction_data[1..]),
        3 => trade(program_id, accounts, &instruction_data[1..]),
        4 => crank(program_id, accounts, &instruction_data[1..]),
        5 => create_market(program_id, accounts, &instruction_data[1..]),
        6 => resolve_market(program_id, accounts, &instruction_data[1..]),
        7 => claim_insurance(program_id, accounts, &instruction_data[1..]),
        8 => pay_insurance(program_id, accounts, &instruction_data[1..]),
        9 => insurance_deposit(program_id, accounts, &instruction_data[1..]),
        10 => insurance_withdraw(program_id, accounts, &instruction_data[1..]),
        11 => create_account(program_id, accounts, &instruction_data[1..]),
        12 => liquidate(program_id, accounts, &instruction_data[1..]),
        13 => admin_withdraw(program_id, accounts, &instruction_data[1..]),
        14 => create_lp(program_id, accounts, &instruction_data[1..]),
        15 => update_lp(program_id, accounts, &instruction_data[1..]),
        16 => deposit_lp(program_id, accounts, &instruction_data[1..]),
        17 => withdraw_lp(program_id, accounts, &instruction_data[1..]),
        18 => trade_no_cpi(program_id, accounts, &instruction_data[1..]),
        19 => crank_no_cpi(program_id, accounts, &instruction_data[1..]),
        20 => liquidate_no_cpi(program_id, accounts, &instruction_data[1..]),
        21 => resolve_market_no_cpi(program_id, accounts, &instruction_data[1..]),
        22 => fund_reward(program_id, accounts, &instruction_data[1..]),
        23 => claim_reward(program_id, accounts, &instruction_data[1..]),
        _ => Err(ProgramError::InvalidInstructionData),
    }
}
</tool_call>
<tool_response>
</tool_response>

Now let me find each handler function and assess its signature requirements. I'll look at each one that could move funds out of the vault.
<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "^fn withdraw"}}
</tool_call>
<tool_response>
Line 1895: fn withdraw(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 1895, "end_line": 1980}}
</tool_call>
<tool_response>
fn withdraw(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let args = WithdrawArgs::try_from_slice(data)?;
    let [state, vault, user, user_token, token_program, user_account] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    validate_state(program_id, state)?;
    validate_vault(state, vault)?;
    let mut state_data = StateData::try_from_slice(&state.data.borrow())?;
    let mut user_account_data = UserAccount::try_from_slice(&user_account.data.borrow())?;
    validate_user_account(state, user, user_account)?;
    if !user.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let amount = args.amount.min(user_account_data.balance);
    let engine = Engine::from(&state_data);
    let (new_state, new_user_account) = engine.withdraw(
        &state_data,
        &user_account_data,
        amount,
    )?;
    state_data = new_state;
    user_account_data = new_user_account;
    state_data.serialize(&mut *state.data.borrow_mut())?;
    user_account_data.serialize(&mut *user_account.data.borrow_mut())?;
    invoke(
        &spl_token::instruction::transfer(
            token_program.key,
            vault.key,
            user_token.key,
            state.key,
            &[],
            amount,
        )?,
        &[vault.clone(), user_token.clone(), state.clone(), token_program.clone()],
    )?;
    Ok(())
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "^fn admin_withdraw"}}
</tool_call>
<tool_response>
Line 3080: fn admin_withdraw(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 3080, "end_line": 3160}}
</tool_call>
<tool_response>
fn admin_withdraw(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let args = AdminWithdrawArgs::try_from_slice(data)?;
    let [state, vault, admin, admin_token, token_program] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    validate_state(program_id, state)?;
    validate_vault(state, vault)?;
    let mut state_data = StateData::try_from_slice(&state.data.borrow())?;
    if !admin.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if state_data.admin != *admin.key {
        return Err(ProgramError::InvalidAccountData);
    }
    let engine = Engine::from(&state_data);
    let new_state = engine.admin_withdraw(&state_data, args.amount)?;
    state_data = new_state;
    state_data.serialize(&mut *state.data.borrow_mut())?;
    invoke(
        &spl_token::instruction::transfer(
            token_program.key,
            vault.key,
            admin_token.key,
            state.key,
            &[],
            args.amount,
        )?,
        &[vault.clone(), admin_token.clone(), state.clone(), token_program.clone()],
    )?;
    Ok(())
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "^fn insurance_withdraw"}}
</tool_call>
<tool_response>
Line 2887: fn insurance_withdraw(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 2887, "end_line": 2960}}
</tool_call>
<tool_response>
fn insurance_withdraw(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let args = InsuranceWithdrawArgs::try_from_slice(data)?;
    let [state, vault, insurance_authority, insurance_token, token_program] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    validate_state(program_id, state)?;
    validate_vault(state, vault)?;
    let mut state_data = StateData::try_from_slice(&state.data.borrow())?;
    if !insurance_authority.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if state_data.insurance_authority != *insurance_authority.key {
        return Err(ProgramError::InvalidAccountData);
    }
    let engine = Engine::from(&state_data);
    let new_state = engine.insurance_withdraw(&state_data, args.amount)?;
    state_data = new_state;
    state_data.serialize(&mut *state.data.borrow_mut())?;
    invoke(
        &spl_token::instruction::transfer(
            token_program.key,
            vault.key,
            insurance_token.key,
            state.key,
            &[],
            args.amount,
        )?,
        &[vault.clone(), insurance_token.clone(), state.clone(), token_program.clone()],
    )?;
    Ok(())
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "^fn withdraw_lp"}}
</tool_call>
<tool_response>
Line 3380: fn withdraw_lp(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 3380, "end_line": 3480}}
</tool_call>
<tool_response>
fn withdraw_lp(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let args = WithdrawLpArgs::try_from_slice(data)?;
    let [state, vault, lp_owner, lp_token, token_program, lp_account] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    validate_state(program_id, state)?;
    validate_vault(state, vault)?;
    let mut state_data = StateData::try_from_slice(&state.data.borrow())?;
    let mut lp_account_data = LpAccount::try_from_slice(&lp_account.data.borrow())?;
    validate_lp_account(state, lp_owner, lp_account)?;
    if !lp_owner.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let engine = Engine::from(&state_data);
    let (new_state, new_lp_account) = engine.withdraw_lp(
        &state_data,
        &lp_account_data,
        args.shares,
    )?;
    state_data = new_state;
    lp_account_data = new_lp_account;
    let amount = state_data.lp_withdraw_amount;
    state_data.serialize(&mut *state.data.borrow_mut())?;
    lp_account_data.serialize(&mut *lp_account.data.borrow_mut())?;
    invoke(
        &spl_token::instruction::transfer(
            token_program.key,
            vault.key,
            lp_token.key,
            state.key,
            &[],
            amount,
        )?,
        &[vault.clone(), lp_token.clone(), state.clone(), token_program.clone()],
    )?;
    Ok(())
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "^fn claim_reward"}}
</tool_call>
<tool_response>
Line 22996, 22998: fn claim_reward(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 22996, "end_line": 23102}}
</tool_call>
<tool_response>
fn claim_reward(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let args = ClaimRewardArgs::try_from_slice(data)?;
    let [state, vault, user, user_token, token_program, user_account] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    validate_state(program_id, state)?;
    validate_vault(state, vault)?;
    let mut state_data = StateData::try_from_slice(&state.data.borrow())?;
    let mut user_account_data = UserAccount::try_from_slice(&user_account.data.borrow())?;
    validate_user_account(state, user, user_account)?;
    if !user.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let engine = Engine::from(&state_data);
    let (new_state, new_user_account) = engine.claim_reward(
        &state_data,
        &user_account_data,
    )?;
    state_data = new_state;
    user_account_data = new_user_account;
    let amount = state_data.reward_withdraw_amount;
    state_data.serialize(&mut *state.data.borrow_mut())?;
    user_account_data.serialize(&mut *user_account.data.borrow_mut())?;
    invoke(
        &spl_token::instruction::transfer(
            token_program.key,
            vault.key,
            user_token.key,
            state.key,
            &[],
            amount,
        )?,
        &[vault.clone(), user_token.clone(), state.clone(), token_program.clone()],
    )?;
    Ok(())
}
</tool_call>
</tool_response>

Now let me look at the engine's `withdraw`, `withdraw_lp`, and `claim_reward` to see if they enforce the invariant:
<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "pub fn withdraw"}}
</tool_call>
<tool_response>
Line 5765: pub fn withdraw(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 5765, "end_line": 5840}}
</tool_call>
<tool_response>
pub fn withdraw(
    &self,
    state: &StateData,
    user_account: &UserAccount,
    amount: u64,
) -> Result<(StateData, UserAccount), ProgramError> {
    let mut state = state.clone();
    let mut user_account = user_account.clone();
    if amount == 0 {
        return Ok((state, user_account));
    }
    // available balance is balance - locked - upnl if negative
    // upnl is computed from the positions
    let im = self.initial_margin(&user_account);
    let upnl = self.unrealized_pnl(&user_account);
    // im includes the margin for all positions
    // we can only withdraw up to balance - im
    let withdrawable = user_account
        .balance
        .saturating_sub(im)
        .saturating_sub(upnl.min(0).unsigned_abs());
    if amount > withdrawable {
        return Err(ProgramError::InvalidArgument);
    }
    user_account.balance = user_account.balance.checked_sub(amount).ok_or(ProgramError::InvalidArgument)?;
    state.balance = state.balance.checked_sub(amount).ok_or(ProgramError::InvalidArgument)?;
    Ok((state, user_account))
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "pub fn withdraw_lp"}}
</tool_call>
<tool_response>
Line 6149: pub fn withdraw_lp(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 6149, "end_line": 6260}}
</tool_call>
<tool_response>
pub fn withdraw_lp(
    &self,
    state: &StateData,
    lp_account: &LpAccount,
    shares: u64,
) -> Result<(StateData, LpAccount), ProgramError> {
    let mut state = state.clone();
    let mut lp_account = lp_account.clone();
    if shares == 0 {
        return Ok((state, lp_account));
    }
    if shares > lp_account.shares {
        return Err(ProgramError::InvalidArgument);
    }
    let total_shares = state.lp_shares;
    if total_shares == 0 {
        return Err(ProgramError::InvalidArgument);
    }
    // lp_balance is the total balance available for lp withdrawals
    // it's the total balance minus what's locked by users
    let lp_balance = state
        .balance
        .checked_sub(state.cash_locked)
        .ok_or(ProgramError::ArithmeticOverflow)?
        .checked_sub(state.claimable_pnl)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    let amount = lp_balance
        .checked_mul(shares)
        .ok_or(ProgramError::ArithmeticOverflow)?
        .checked_div(total_shares)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    lp_account.shares = lp_account
        .shares
        .checked_sub(shares)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.lp_shares = state
        .lp_shares
        .checked_sub(shares)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.balance = state
        .balance
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.lp_withdraw_amount = amount;
    Ok((state, lp_account))
}
</tool_call>
</tool_response>

`withdraw_lp` explicitly subtracts `cash_locked` and `claimable_pnl` before computing the withdrawal amount. But it does **not** subtract `insurance.balance`. Let me check how insurance is tracked:
<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "insurance_balance"}}
</tool_call>
<tool_response>
Line 127: insurance_balance: u64,
Line 149: insurance_balance: u64,
Line 208: insurance_balance: u64,
Line 211: pub insurance_balance: u64,
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 100, "end_line": 250}}
</tool_call>
<tool_response>
use std::io::Write;

use borsh::{BorshDeserialize, BorshSerialize};
use solana_program::{
    account_info::AccountInfo,
    entrypoint::ProgramResult,
    program::invoke,
    program_error::ProgramError,
    pubkey::Pubkey,
};

#[cfg(test)]
use solana_program::clock::Clock;

#[cfg(test)]
use solana_program::sysvar::Sysvar;

use spl_token;

#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Default)]
pub struct StateData {
    pub admin: Pubkey,
    pub insurance_authority: Pubkey,
    pub insurance_operator: Pubkey,
    pub vault: Pubkey,
    pub balance: u64,
    pub cash_locked: u64,
    pub claimable_pnl: u64,
    pub insurance_balance: u64,
    pub lp_shares: u64,
    pub lp_withdraw_amount: u64,
    pub reward_withdraw_amount: u64,
    pub reward_balance: u64,
    pub markets: [MarketData; 8],
}
</tool_call>
</tool_response>

Now I have the key structure. `insurance_balance` is a separate field in `StateData`. Let me check `withdraw_lp` again — it computes:

```
lp_balance = state.balance - state.cash_locked - state.claimable_pnl
```

It does **not** subtract `state.insurance_balance`. Let me verify what `insurance_deposit` and `insurance_withdraw` do to `state.balance`:
<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "pub fn insurance_deposit\|pub fn insurance_withdraw"}}
</tool_call>
<tool_response>
Line 6060: pub fn insurance_deposit(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 6060, "end_line": 6150}}
</tool_call>
<tool_response>
pub fn insurance_deposit(
    &self,
    state: &StateData,
    amount: u64,
) -> Result<StateData, ProgramError> {
    let mut state = state.clone();
    state.balance = state
        .balance
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.insurance_balance = state
        .insurance_balance
        .checked_add(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    Ok(state)
}
</tool_call>
</tool_response>

<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "pub fn insurance_withdraw"}}
</tool_call>
<tool_response>
Line 6098: pub fn insurance_withdraw(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 6098, "end_line": 6149}}
</tool_call>
<tool_response>
pub fn insurance_withdraw(
    &self,
    state: &StateData,
    amount: u64,
) -> Result<StateData, ProgramError> {
    let mut state = state.clone();
    if amount > state.insurance_balance {
        return Err(ProgramError::InvalidArgument);
    }
    state.balance = state
        .balance
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.insurance_balance = state
        .insurance_balance
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    Ok(state)
}
</tool_call>
</tool_response>

Now I see the full picture. `insurance_deposit` adds to **both** `balance` and `insurance_balance`. So `balance` includes insurance funds. But `withdraw_lp` computes:

```
lp_balance = balance - cash_locked - claimable_pnl
```

This **includes** `insurance_balance` in the withdrawable pool. Let me verify with a concrete scenario. After `insurance_deposit(X)`:
- `state.balance = user_deposits + X`
- `state.insurance_balance = X`

Then `withdraw_lp` allows withdrawing up to `(user_deposits + X) - cash_locked - claimable_pnl`, which **includes X (insurance funds)**.

Now let me check `claim_reward`:
<tool_call>
{"name": "search_files", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "pattern": "pub fn claim_reward"}}
</tool_call>
<tool_response>
Line 22906, 22930: pub fn claim_reward(
</tool_response>

<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator.rs", "start_line": 22906, "end_line": 22996}}
</tool_call>
<tool_response>
pub fn claim_reward(
    &self,
    state: &StateData,
    user_account: &UserAccount,
) -> Result<(StateData, UserAccount), ProgramError> {
    let mut state = state.clone();
    let mut user_account = user_account.clone();
    let amount = user_account.reward_balance;
    if amount == 0 {
        return Ok((state, user_account));
    }
    if amount > state.reward_balance {
        return Err(ProgramError::InvalidArgument);
    }
    state.balance = state
        .balance
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    state.reward_balance = state
        .reward_balance
        .checked_sub(amount)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    user_account.reward_balance = 0;
    state.reward_withdraw_amount = amount;
    Ok((state, user_account))
}
</tool_call>
</tool_