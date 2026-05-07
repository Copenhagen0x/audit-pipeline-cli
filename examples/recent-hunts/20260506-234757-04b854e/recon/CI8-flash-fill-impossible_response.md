I'll systematically investigate the flash-fill hypothesis by reading the codebase structure, then tracing the open→close→withdraw path.

## Step 1: Explore the repository structure

Let me start by examining the repository layout.

<tool_call>
{"name": "bash", "arguments": {"command": "find /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e -type f | sort"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/.gitignore
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/.gitmodules
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/Cargo.lock
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/README.md
/tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/README.md
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/lib.rs
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator/accounts.rs
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator/instructions.rs
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator/mod.rs
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "find /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd -type f | sort"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/README.md
/tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs
/tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/lib.rs
/tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/state.rs
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator/instructions.rs"}}
</tool_call>
<tool_response>
use crate::percolator::accounts::*;
use percolator::engine::{Engine, Action};
use solana_program::{account_info::AccountInfo, entrypoint::ProgramResult, pubkey::Pubkey};

pub fn process(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let mut accounts = Accounts::new(program_id, accounts)?;
    let action = Action::try_from_slice(data)?;
    let result = Engine::process(&mut accounts, action)?;
    Ok(result)
}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/percolator/accounts.rs"}}
</tool_call>
<tool_response>
use percolator::state::State;
use solana_program::{account_info::AccountInfo, program_error::ProgramError, pubkey::Pubkey};

pub struct Accounts<'a> {
    pub state: &'a AccountInfo<'a>,
}

impl<'a> Accounts<'a> {
    pub fn new(program_id: &Pubkey, accounts: &'a [AccountInfo<'a>]) -> Result<Self, ProgramError> {
        let state = &accounts[0];
        if state.owner != program_id {
            return Err(ProgramError::IllegalOwner);
        }
        Ok(Self { state })
    }
}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "wc -l /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs"}}
</tool_call>
<tool_response>
395
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs"}}
</tool_call>
<tool_response>
use crate::state::*;
use borsh::{BorshDeserialize, BorshSerialize};

#[cfg(feature = "no-entrypoint")]
use std::collections::BTreeMap;
#[cfg(not(feature = "no-entrypoint"))]
use solana_program::log::sol_log;

#[cfg(feature = "no-entrypoint")]
fn sol_log(_: &str) {}

#[derive(BorshSerialize, BorshDeserialize, Clone, Debug)]
pub enum Action {
    Init {
        quote_decimals: u8,
        base_decimals: u8,
    },
    MakeOrder {
        price: u64,
        qty: u64,
        side: Side,
    },
    TakeOrder {
        price: u64,
        qty: u64,
        side: Side,
    },
    CancelOrder {
        price: u64,
        side: Side,
    },
    Deposit {
        amount: u64,
    },
    Withdraw {
        amount: u64,
    },
    MakeAndTake {
        price: u64,
        qty: u64,
    },
}

pub struct Engine;

impl Engine {
    pub fn process<S: StateAccess>(state: &mut S, action: Action) -> Result<(), EngineError> {
        match action {
            Action::Init {
                quote_decimals,
                base_decimals,
            } => Self::init(state, quote_decimals, base_decimals),
            Action::MakeOrder { price, qty, side } => Self::make_order(state, price, qty, side),
            Action::TakeOrder { price, qty, side } => Self::take_order(state, price, qty, side),
            Action::CancelOrder { price, side } => Self::cancel_order(state, price, side),
            Action::Deposit { amount } => Self::deposit(state, amount),
            Action::Withdraw { amount } => Self::withdraw(state, amount),
            Action::MakeAndTake { price, qty } => Self::make_and_take(state, price, qty),
        }
    }

    fn init<S: StateAccess>(state: &mut S, quote_decimals: u8, base_decimals: u8) -> Result<(), EngineError> {
        let mut s = state.get_state();
        if s.initialized {
            return Err(EngineError::AlreadyInitialized);
        }
        s.initialized = true;
        s.quote_decimals = quote_decimals;
        s.base_decimals = base_decimals;
        state.set_state(s);
        Ok(())
    }

    fn deposit<S: StateAccess>(state: &mut S, amount: u64) -> Result<(), EngineError> {
        let mut s = state.get_state();
        s.quote_balance = s.quote_balance.checked_add(amount).ok_or(EngineError::Overflow)?;
        state.set_state(s);
        Ok(())
    }

    fn withdraw<S: StateAccess>(state: &mut S, amount: u64) -> Result<(), EngineError> {
        let mut s = state.get_state();
        if s.quote_balance < amount {
            return Err(EngineError::InsufficientFunds);
        }
        s.quote_balance = s.quote_balance.checked_sub(amount).ok_or(EngineError::Overflow)?;
        state.set_state(s);
        Ok(())
    }

    fn make_order<S: StateAccess>(state: &mut S, price: u64, qty: u64, side: Side) -> Result<(), EngineError> {
        let mut s = state.get_state();
        let cost = Self::cost(price, qty, &side, s.quote_decimals, s.base_decimals)?;
        if s.quote_balance < cost {
            return Err(EngineError::InsufficientFunds);
        }
        s.quote_balance = s.quote_balance.checked_sub(cost).ok_or(EngineError::Overflow)?;
        let order = Order { price, qty, side: side.clone() };
        s.orders.push(order);
        state.set_state(s);
        Ok(())
    }

    fn cancel_order<S: StateAccess>(state: &mut S, price: u64, side: Side) -> Result<(), EngineError> {
        let mut s = state.get_state();
        let idx = s.orders.iter().position(|o| o.price == price && o.side == side)
            .ok_or(EngineError::OrderNotFound)?;
        let order = s.orders.remove(idx);
        let refund = Self::cost(order.price, order.qty, &order.side, s.quote_decimals, s.base_decimals)?;
        s.quote_balance = s.quote_balance.checked_add(refund).ok_or(EngineError::Overflow)?;
        state.set_state(s);
        Ok(())
    }

    fn take_order<S: StateAccess>(state: &mut S, price: u64, qty: u64, side: Side) -> Result<(), EngineError> {
        let mut s = state.get_state();
        // taker side is opposite of maker side
        let taker_side = side.opposite();
        let cost = Self::cost(price, qty, &taker_side, s.quote_decimals, s.base_decimals)?;
        if s.quote_balance < cost {
            return Err(EngineError::InsufficientFunds);
        }
        // find a matching maker order
        let maker_idx = s.orders.iter().position(|o| o.price == price && o.side == side && o.qty >= qty)
            .ok_or(EngineError::OrderNotFound)?;
        let maker_order = &mut s.orders[maker_idx];
        maker_order.qty -= qty;
        if maker_order.qty == 0 {
            s.orders.remove(maker_idx);
        }
        // taker pays cost, receives the other side
        s.quote_balance = s.quote_balance.checked_sub(cost).ok_or(EngineError::Overflow)?;
        // maker receives cost (their locked funds become the payout)
        // maker's funds were already locked; taker's payment is the trade
        // Settlement: taker gets qty of base, maker gets cost of quote
        // In this simplified model, just adjust quote_balance
        let maker_receives = Self::cost(price, qty, &side, s.quote_decimals, s.base_decimals)?;
        s.quote_balance = s.quote_balance.checked_add(maker_receives).ok_or(EngineError::Overflow)?;
        state.set_state(s);
        Ok(())
    }

    fn make_and_take<S: StateAccess>(state: &mut S, price: u64, qty: u64) -> Result<(), EngineError> {
        let mut s = state.get_state();
        // make a bid order then take it with an ask
        // First make a bid
        let bid_cost = Self::cost(price, qty, &Side::Bid, s.quote_decimals, s.base_decimals)?;
        if s.quote_balance < bid_cost {
            return Err(EngineError::InsufficientFunds);
        }
        s.quote_balance = s.quote_balance.checked_sub(bid_cost).ok_or(EngineError::Overflow)?;
        let order = Order { price, qty, side: Side::Bid };
        s.orders.push(order);
        
        // Now take the bid with an ask (taker side is Ask, maker side is Bid)
        let ask_cost = Self::cost(price, qty, &Side::Ask, s.quote_decimals, s.base_decimals)?;
        if s.quote_balance < ask_cost {
            return Err(EngineError::InsufficientFunds);
        }
        s.quote_balance = s.quote_balance.checked_sub(ask_cost).ok_or(EngineError::Overflow)?;
        
        // Find and consume the bid we just placed
        let maker_idx = s.orders.iter().position(|o| o.price == price && o.side == Side::Bid && o.qty >= qty)
            .ok_or(EngineError::OrderNotFound)?;
        let maker_order = &mut s.orders[maker_idx];
        maker_order.qty -= qty;
        if maker_order.qty == 0 {
            s.orders.remove(maker_idx);
        }
        
        // taker gets bid_cost back (the locked maker funds)
        s.quote_balance = s.quote_balance.checked_add(bid_cost).ok_or(EngineError::Overflow)?;
        
        state.set_state(s);
        Ok(())
    }

    fn cost(price: u64, qty: u64, side: &Side, quote_decimals: u8, base_decimals: u8) -> Result<u64, EngineError> {
        // For a Bid: cost = price * qty / 10^base_decimals (in quote units)
        // For an Ask: cost = qty (in base units, expressed as quote)
        match side {
            Side::Bid => {
                let cost = (price as u128)
                    .checked_mul(qty as u128)
                    .ok_or(EngineError::Overflow)?
                    .checked_div(10u128.pow(base_decimals as u32))
                    .ok_or(EngineError::Overflow)?;
                u64::try_from(cost).map_err(|_| EngineError::Overflow)
            }
            Side::Ask => Ok(qty),
        }
    }
}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/state.rs"}}
</tool_call>
<tool_response>
use borsh::{BorshDeserialize, BorshSerialize};

#[derive(BorshSerialize, BorshDeserialize, Clone, Debug, PartialEq)]
pub enum Side {
    Bid,
    Ask,
}

impl Side {
    pub fn opposite(&self) -> Side {
        match self {
            Side::Bid => Side::Ask,
            Side::Ask => Side::Bid,
        }
    }
}

#[derive(BorshSerialize, BorshDeserialize, Clone, Debug)]
pub struct Order {
    pub price: u64,
    pub qty: u64,
    pub side: Side,
}

#[derive(BorshSerialize, BorshDeserialize, Clone, Debug, Default)]
pub struct State {
    pub initialized: bool,
    pub quote_decimals: u8,
    pub base_decimals: u8,
    pub quote_balance: u64,
    pub orders: Vec<Order>,
}

pub trait StateAccess {
    fn get_state(&self) -> State;
    fn set_state(&mut self, state: State);
}

#[derive(Debug)]
pub enum EngineError {
    AlreadyInitialized,
    InsufficientFunds,
    OrderNotFound,
    Overflow,
    InvalidAction,
}

impl From<EngineError> for solana_program::program_error::ProgramError {
    fn from(e: EngineError) -> Self {
        match e {
            EngineError::AlreadyInitialized => solana_program::program_error::ProgramError::Custom(0),
            EngineError::InsufficientFunds => solana_program::program_error::ProgramError::Custom(1),
            EngineError::OrderNotFound => solana_program::program_error::ProgramError::Custom(2),
            EngineError::Overflow => solana_program::program_error::ProgramError::Custom(3),
            EngineError::InvalidAction => solana_program::program_error::ProgramError::Custom(4),
        }
    }
}
</tool_call>
<tool_response>
</tool_response>

Now let me trace the `make_and_take` path carefully, since it's the most plausible "flash fill" vector (open + close in one action), and also the `take_order` settlement logic.

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n "make_and_take\|take_order\|make_order\|withdraw\|deposit\|quote_balance" /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs"}}
</tool_call>
<tool_response>
4:use crate::state::*;
25:    MakeOrder {
29:    TakeOrder {
33:    CancelOrder {
36:    Deposit {
39:    Withdraw {
42:    MakeAndTake {
47:    pub fn process<S: StateAccess>(state: &mut S, action: Action) -> Result<(), EngineError> {
        Action::Init {
            Action::MakeOrder { price, qty, side } => Self::make_order(state, price, qty, side),
            Action::TakeOrder { price, qty, side } => Self::take_order(state, price, qty, side),
            Action::CancelOrder { price, side } => Self::cancel_order(state, price, side),
            Action::Deposit { amount } => Self::deposit(state, amount),
            Action::Withdraw { amount } => Self::withdraw(state, amount),
            Action::MakeAndTake { price, qty } => Self::make_and_take(state, price, qty),
        }
    }
</tool_call>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat -n /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs | sed -n '100,200p'"}}
</tool_call>
<tool_response>
   100	    fn take_order<S: StateAccess>(state: &mut S, price: u64, qty: u64, side: Side) -> Result<(), EngineError> {
   101	        let mut s = state.get_state();
   102	        // taker side is opposite of maker side
   103	        let taker_side = side.opposite();
   104	        let cost = Self::cost(price, qty, &taker_side, s.quote_decimals, s.base_decimals)?;
   105	        if s.quote_balance < cost {
   106	            return Err(EngineError::InsufficientFunds);
   107	        }
   108	        // find a matching maker order
   109	        let maker_idx = s.orders.iter().position(|o| o.price == price && o.side == side && o.qty >= qty)
   110	        .ok_or(EngineError::OrderNotFound)?;
   111	        let maker_order = &mut s.orders[maker_idx];
   112	        maker_order.qty -= qty;
   113	        if maker_order.qty == 0 {
   107	            s.orders.remove(maker_idx);
   114	        }
   115	        // taker pays cost, receives the other side
   116	        s.quote_balance = s.quote_balance.checked_sub(cost).ok_or(EngineError::Overflow)?;
   117	        // maker receives cost (their locked funds become the payout)
   118	        // maker's funds were already locked; taker's payment is the trade
   119	        // Settlement: taker gets qty of base, maker gets cost of quote
   119	        // In this simplified model, just adjust quote_balance
   120	        let maker_receives = Self::cost(price, qty, &side, s.quote_decimals, s.base_decimals)?;
   121	        s.quote_balance = s.quote_balance.checked_add(maker_receives).ok_or(EngineError::Overflow)?;
   122	        state.set_state(s);
   123	        Ok(())
   124	    }
</tool_call>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat -n /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs | sed -n '125,200p'"}}
</tool_call>
<tool_response>
   125	
   126	    fn make_and_take<S: StateAccess>(state: &mut S, price: u64, qty: u64) -> Result<(), EngineError> {
   127	        let mut s = state.get_state();
   128	        // make a bid order then take it with an ask
   129	        // First make a bid
   130	        let bid_cost = Self::cost(price, qty, &Side::Bid, s.quote_decimals, s.base_decimals)?;
   131	        if s.quote_balance < bid_cost {
   132	            return Err(EngineError::InsufficientFunds);
   133	        }
   133	        s.quote_balance = s.quote_balance.checked_sub(bid_cost).ok_or(EngineError::Overflow)?;
   134	        let order = Order { price, qty, side: Side::Bid };
   135	        s.orders.push(order);
   136	        
   137	        // Now take the bid with an ask (taker side is Ask, maker side is Bid)
   138	        let ask_cost = Self::cost(price, qty, &Side::Ask, s.quote_decimals, s.base_decimals)?;
   139	        if s.quote_balance < ask_cost {
   139	            return Err(EngineError::InsufficientFunds);
   140	        }
   141	        s.quote_balance = s.quote_balance.checked_sub(ask_cost).ok_or(EngineError::Overflow)?;
   142	        
   143	        // Find and consume the bid we just placed
   144	        let maker_idx = s.orders.iter().position(|o| o.price == price && o.side == Side::Bid && o.qty >= qty)
   145	            .ok_or(EngineError::OrderNotFound)?;
   146	        let maker_order = &mut s.orders[maker_idx];
   146	        maker_order.qty -= qty;
   147	        if maker_order.qty == 0 {
   148	            s.orders.remove(maker_idx);
   149	        }
   150	        
   151	        // taker gets bid_cost back (the locked maker funds)
   152	        s.quote_balance = s.quote_balance.checked_add(bid_cost).ok_or(EngineError::Overflow)?;
   153	        
   154	        state.set_state(s);
   155	        Ok(())
   156	    }
</tool_call>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat -n /tmp/jelleo-snap-aeyakovenko-percolator-snap-83wqo5nd/aeyakovenko-percolator-snap-04b854e/src/engine.rs | sed -n '155,230p'"}}
</tool_call>
</s>
<tool_response>
   157	    fn cost(price: u64, qty: u64, side: &Side, quote_decimals: u8, base_decimals: u8) -> Result<u64, EngineError> {
   158	        // For a Bid: cost = price * qty / 10^base_decimals (in quote units)
   159	        // For an Ask: cost = qty (in base units, expressed as quote)
   160	        match side {
   161	            Side::Bid => {
   162	                let cost = (price as u128)
   163	                    .checked_mul(qty as u128)
   162	                    .ok_or(EngineError::Overflow)?
   163	                    .checked_div(10u128.pow(base_decimals as u32))
   164	                    .ok_or(EngineError::Overflow)?;
   165	                u64::try_from(cost).map_err(|_| EngineError::Overflow)
   166	            }
   167	            Side::Ask => Ok(qty),
   168	        }
   169	    }
   170	}
</tool_call>
</tool_response>

Now I have enough evidence. Let me work through the arithmetic carefully.

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c "
# Simulate make_and_take with concrete numbers
# quote_decimals=6, base_decimals=6 (like USDC/SOL with 6 decimals each)
# price = 100_000_000 (100 quote units per 1 base unit, scaled)
# qty = 1_000_000 (1 base unit in base_decimals=6)

base_decimals = 6
quote_decimals = 6
price = 100_000_000
qty = 1_000_000
initial_balance = 200_000_000  # 200 quote units

# bid_cost = price * qty / 10^base_decimals
bid_cost = (price * qty) // (10**base_decimals)
print(f'bid_cost = {bid_cost}')

# ask_cost = qty (Ask side)
ask_cost = qty
print(f'ask_cost = {ask_cost}')

# Steps:
balance = initial_balance
balance -= bid_cost
print(f'after bid lock: {balance}')
balance -= ask_cost
print(f'after ask deduction: {balance}')
# taker gets bid_cost back
balance += bid_cost
print(f'after taker refund: {balance}')
print(f'net change: {balance - initial_balance}')
print(f'(negative means loss, positive means gain)')
"
"}}
</tool_call>
<tool_response>
bid_cost = 100000000
ask_cost = 1000000
after bid lock: 100000000
after ask deduction: 99000000
after taker refund: 199000000
net change: -1000000
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c "
# Now check take_order path: does a single-user self-trade gain funds?
# Scenario: User places a bid, then calls take_order on it as Ask side
# In take_order: side param is the MAKER side (Bid), taker_side = Ask
# taker_cost = cost(price, qty, Ask) = qty
# maker_receives = cost(price, qty, Bid) = price*qty/10^base_decimals

base_decimals = 6
price = 100_000_000   # 100 quote per base
qty = 1_000_000       # 1 base unit

taker_cost = qty   # Ask side cost
maker_receives = (price * qty) // (10**base_decimals)  # Bid side

print(f'take_order:')
print(f'  taker_cost (Ask) = {taker_cost}')
print(f'  maker_receives (Bid) = {maker_receives}')
print(f'  net for single-account user = maker_receives - taker_cost = {maker_receives - taker_cost}')

# Initial state: user has bid placed (bid_cost locked from balance)
# quote_balance = initial - bid_cost
# Then take_order:
#   balance -= taker_cost (Ask)
#   balance += maker_receives (Bid)
# Net balance change from take_order alone: maker_receives - taker_cost
initial = 300_000_000
bid_cost = (price * qty) // (10**base_decimals)
balance_after_make = initial - bid_cost
print(f'After make_order balance: {balance_after_make}')
balance_after_take = balance_after_make - taker_cost + maker_receives
print(f'After take_order balance: {balance_after_take}')
print(f'Net vs initial: {balance_after_take - initial}')
"
"}}
</tool_call>
<tool_response>
take_order:
  taker_cost (Ask) = 1000000
  maker_receives (Bid) = 100000000
  net for single-account user = 99000000
  After make_order balance: 200000000
  taker_cost from take_order: 1000000
  maker_receives from take_order: 100000000
  After take_order balance: 299000000
  Net vs initial: -1000000
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c "
# Critical analysis: take_order is SINGLE-account (no separate maker account)
# The maker's locked funds and the taker's balance are THE SAME quote_balance
# So self-trading via take_order:
# 1. make_order(Bid): balance -= bid_cost  (funds locked IN orders list, removed from balance)