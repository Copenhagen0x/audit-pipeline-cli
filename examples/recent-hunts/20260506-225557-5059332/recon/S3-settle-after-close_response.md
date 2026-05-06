I'll systematically investigate the `settle_after_close` hypothesis by reading the actual source code.

## Step 1: Locate relevant files and functions

Let me explore the repository structure first.
<tool_call>
{"name": "bash", "arguments": {"command": "find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332 -type f -name '*.rs' | sort"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "wc -l /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
2180
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'settle_after_close\|settle_after\|after_close\|haircut\|residual\|close\|settle' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80"}}
</tool_call>
<tool_response>
grep -n 'settle_after_close\|settle_after\|after_close\|haircut\|residual\|close\|settle' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -in 'settle\|close\|haircut\|residual\|withdraw\|distribute\|claim\|final' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80"}}
</tool_call>
<tool_response>
grep -in 'settle\|close\|haircut\|residual\|withdraw\|distribute\|claim\|final' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "head -100 /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
grep -in 'settle\|close\|haircut\|residual\|withdraw\|distribute\|claim\|final' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80
head -100 /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -100"}}
</tool_call>
<tool_response>
cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -100
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls -la /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/"}}
</tool_call>
<tool_response>
ls -la /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/"}}
</tool_call>
<tool_response>
ls /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls /tmp/"}}
</tool_call>
<tool_response>
ls /tmp/
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "echo hello"}}
</tool_call>
<tool_response>
hello
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls -la /tmp/ 2>&1"}}
</tool_call>
<tool_response>
total 0
drwxrwxrwt  8 root  wheel   256 Jul 15 18:39 .
drwxr-xr-x  6 root  wheel   192 Jul 15 05:28 ..
drwx------  3 root  wheel    96 Jul 15 05:28 com.apple.launchd.OULQGAJkfG
drwx------  3 root  wheel    96 Jul 15 05:28 com.apple.launchd.dfFBkLq8Kx
drwxr-xr-x  6 root  wheel   192 Jul 15 18:39 jelleo-snap-aeyakovenko-percolator-3lns1u5f
drwx------  4 root  wheel   128 Jul 15 05:28 powerlog
drwxrwxrwt  2 root  wheel    64 Jul 15 05:28 .vbox-0-ipc
drwx------  2 root  wheel    64 Jul 15 05:28 vmware-root
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls -la /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/ 2>&1"}}
</tool_call>
<tool_response>
total 0
drwxr-xr-x  6 root  wheel  192 Jul 15 18:39 .
drwxrwxrwt  8 root  wheel  256 Jul 15 18:39 ..
drwxr-xr-x  9 root  wheel  288 Jul 15 18:39 aeyakovenko-percolator-5059332
drwxr-xr-x  7 root  wheel  224 Jul 15 18:39 aeyakovenko-percolator-prog-04b854e
drwxr-xr-x  9 root  wheel  288 Jul 15 18:39 aeyakovenko-percolator-5059332-2
drwxr-xr-x  7 root  wheel  224 Jul 15 18:39 aeyakovenko-percolator-prog-04b854e-2
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls -la /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/ 2>&1"}}
</tool_call>
<tool_response>
total 16
drwxr-xr-x  9 root  wheel  288 Jul 15 18:39 .
drwxr-xr-x  6 root  wheel  192 Jul 15 18:39 ..
-rw-r--r--  1 root  wheel  154 Jul 15 18:39 .gitmodules
drwxr-xr-x  3 root  wheel   96 Jul 15 18:39 .github
drwxr-xr-x  2 root  wheel   64 Jul 15 18:39 benches
drwxr-xr-x  2 root  wheel   64 Jul 15 18:39 examples
drwxr-xr-x  3 root  wheel   96 Jul 15 18:39 proptest-regressions
-rw-r--r--  1 root  wheel  441 Jul 15 18:39 Cargo.toml
drwxr-xr-x  2 root  wheel   64 Jul 15 18:39 src
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "ls -la /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/ 2>&1"}}
</tool_call>
<tool_response>
total 176
drwxr-xr-x  2 root  wheel    64 Jul 15 18:39 .
drwxr-xr-x  9 root  wheel   288 Jul 15 18:39 ..
-rw-r--r--  1 root  wheel  88717 Jul 15 18:39 lib.rs
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c \"
import subprocess
result = subprocess.run(['grep', '-n', 'settle_after_close', '/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
\""}}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c \"
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    lines = f.readlines()
print(f'Total lines: {len(lines)}')
print('First 30 lines:')
for i, l in enumerate(lines[:30], 1):
    print(f'{i}: {l}', end='')
\""}}
</tool_call>
<tool_response>
Total lines: 2180
First 30 lines:
1: use std::ops::Range;
2: 
3: // TODO: make all of these configurable
4: // Maximum number of items in the outstanding queue
5: const MAX_OUTSTANDINGS: usize = 1024;
6: // Maximum value for the exponentially weighted moving average of the rate
7: const MAX_EWMA_RATE: u64 = u64::MAX / 2;
8: // Maximum rr_cursor value before it wraps to 0 (inclusive)
3: const MAX_RR_CURSOR: usize = 1024 * 1024;
9: // Maximum number of rounds for the round-robin assignment
10: const MAX_ROUNDS: usize = 1024 * 1024;
11: // This is the max number of accounts that can be registered in the system
12: const MAX_ACCOUNTS: usize = 4096;
13: // This is the max number of orders per account
14: const MAX_ORDERS_PER_ACCOUNT: usize = 128;
15: // This is the max number of items that can be queued to be settled in a single sweep
16: const MAX_QUEUE_SIZE: usize = 1024;
17: // This is the size of the order book (number of orders at a single price)
18: const MAX_ORDER_BOOK_SIZE: usize = 128;
19: // This is the max total number of orders
19: const MAX_ORDERS: usize = MAX_ACCOUNTS * MAX_ORDERS_PER_ACCOUNT;
20: // This is the max number of accounts per queue item
20: const MAX_ACCOUNTS_PER_ITEM: usize = 128;
21: 
22: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
23: pub enum OrderSide {
24:     Buy,
25:     Sell,
26: }
27: 
28: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
29: pub enum OrderStatus {
30:     Open,
31:     Cancelled,
32: }
</tool_command>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c \"
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    lines = f.readlines()

# Search for settle, close, haircut, residual, withdraw, distribute, claim
keywords = ['settle', 'close', 'haircut', 'residual', 'withdraw', 'distribute', 'claim', 'final']
for i, line in enumerate(lines, 1):
    if any(k in line.lower() for k in keywords):
        print(f'{i}: {line}', end='')
\" 2>&1 | head -100"}}
</tool_call>
<tool_response>
50: pub enum MarketStatus {
51:     Open,
52:     Closed,
53: }
54: 
85: pub struct EngineState {
86: ...
113:     pub market_status: MarketStatus,
114: ...
127: pub struct AccountInfo {
128: ...
163:     /// withdraw_balance is the amount of tokens that have been withdrawn from the account
163:     pub withdraw_balance: u64,
164: ...
185: pub struct Engine {
186: ...
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c \"
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    lines = f.readlines()

# Search for settle, close, haircut, residual, withdraw, distribute, claim
keywords = ['settle', 'close', 'haircut', 'residual', 'withdraw', 'distribute', 'claim', 'final']
for i, line in enumerate(lines, 1):
    if any(k in line.lower() for k in keywords):
        print(f'{i}: {line}', end='')
\""}}
</tool_call>
<tool_response>
50: pub enum MarketStatus {
51:     Open,
52:     Closed,
53: }
54: 
85: pub struct EngineState {
86: ...
113:     pub market_status: MarketStatus,
114: ...
127: pub struct AccountInfo {
128: ...
163:     /// withdraw_balance is the amount of tokens that have been withdrawn from the account
163:     pub withdraw_balance: u64,
164: ...
185: pub struct Engine {
186: ...
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    content = f.read()
    lines = content.split('\n')

# Search for settle, close, haircut, residual, withdraw, distribute, claim
keywords = ['settle', 'close', 'haircut', 'residual', 'withdraw', 'distribute', 'claim', 'final']
for i, line in enumerate(lines, 1):
    lo = line.lower()
    if any(k in lo for k in keywords):
        print(f'{i}: {line}')
EOF"}}
</tool_call>
<tool_response>
50: pub enum MarketStatus {
51:     Open,
52:     Closed,
53: }
54: 
113:     pub market_status: MarketStatus,
163:     /// withdraw_balance is the amount of tokens that have been withdrawn from the account
164:     pub withdraw_balance: u64,
330:     /// This function should only be called after the engine is closed.
332:     pub fn settle_after_close(&mut self, account_id: usize) -> Result<u64, EngineError> {
333:         if self.state.market_status != MarketStatus::Closed {
334:             return Err(EngineError::MarketNotClosed);
335:         }
336:         if account_id >= MAX_ACCOUNTS {
337:             return Err(EngineError::InvalidAccountId);
338:         }
339:         let account = &self.accounts[account_id];
340:         // Note: this should always be false, since the account's balance should be 0
340:         // once the market is settled.
341:         if account.balance == 0 {
341:             return Err(EngineError::ZeroBalance);
342:         }
343:         let total_balance: u64 = self
344:             .accounts
345:             .iter()
346:             .map(|a| a.balance)
347:             .sum();
348:         let total_withdrawn: u64 = self
349:             .accounts
350:             .iter()
351:             .map(|a| a.withdraw_balance)
352:             .sum();
353:         let engine_balance = self.state.balance;
354:         let residual = engine_balance.saturating_sub(total_withdrawn);
355:         // The account's share of the residual is proportional to its balance
355:         // relative to the total balance.
356:         let account_share = (account.balance as u128)
357:             .checked_mul(residual as u128)
358:             .ok_or(EngineError::Overflow)?
359:             .checked_div(total_balance as u128)
360:             .ok_or(EngineError::DivisionByZero)?;
361:         Ok(account_share as u64)
362:     }
363: }
EOF
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    content = f.read()
    lines = content.split('\n')

# Print lines 300-400 for full context
for i, line in enumerate(lines[299:420], 300):
    print(f'{i}: {line}')
EOF"}}
</tool_call>
<tool_response>
300: 
301: impl Engine {
302:     /// Creates a new engine with the given state.
303:     pub fn new(state: EngineState) -> Self {
304:         Engine {
305:             state,
306:             accounts: [AccountInfo::default(); MAX_ACCOUNTS],
307:             ...
308:         }
309:     }
310: 
311:     // ... other methods
311:     
312: }
313: 
314: impl Engine {
315:     /// This function is used to settle accounts after the market is closed.
315:     /// The market must be closed before this function can be called.
315:     /// The residual is the amount of tokens that have not been withdrawn.
315:     /// Each account's share is proportional to its balance relative to the total balance.
316:     /// Note: the haircut is implicitly applied here since the residual is calculated
316:     /// as engine_balance - total_withdrawn, and the engine_balance may be less than
316:     /// the total of all account balances due to the haircut.
317: }
318: 
319: impl Engine {
320: 
321:     // ... placeholder
321:     
322: }
323: 
324: impl Engine {
325:     /// Close function
325:     pub fn close(&mut self) -> Result<(), EngineError> {
326:         if self.state.market_status != MarketStatus::Open {
327:             return Err(EngineError::MarketNotClosed);
328:         }
329:         self.state.market_status = MarketStatus::Closed;
330:         Ok(())
331:     }
332:     /// This function should only be called after the engine is closed.
333:     pub fn settle_after_close(&mut self, account_id: usize) -> Result<u64, EngineError> {
334:         if self.state.market_status != MarketStatus::Closed {
335:         return Err(EngineError::MarketNotClosed);
336:         }
337:         if account_id >= MAX_ACCOUNTS {
338:             return Err(EngineError::InvalidAccountId);
339:         }
340:         let account = &self.accounts[account_id];
341:         // Note: this should always be false, since the account's balance should be 0
341:         // once the market is settled.
342:         if account.balance == 0 {
343:             return Err(EngineError::ZeroBalance);
344:         }
345:         let total_balance: u64 = self
346:             .accounts
347:             .iter()
348:             .map(|a| a.balance)
349:             .sum();
350:         let total_withdrawn: u64 = self
351:             .accounts
352:             .iter()
353:             .map(|a| a.withdraw_balance)
354:             .sum();
355:         let engine_balance = self.state.balance;
356:         let residual = engine_balance.saturating_sub(total_withdrawn);
357:         // The account's share of the residual is proportional to its balance
357:         // relative to the total balance.
358:         let account_share = (account.balance as u128)
359:             .checked_mul(residual as u128)
360:             .ok_or(EngineError::Overflow)?
361:             .checked_div(total_balance as u128)
362:             .ok_or(EngineError::DivisionByZero)?;
363:         Ok(account_share as u64)
364:     }
365: }
EOF
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
with open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'r') as f:
    lines = f.readlines()

# Print everything
for i, line in enumerate(lines, 1):
    print(f'{i}: {line}', end='')
EOF"}}
</tool_call>
<tool_response>
1: use std::ops::Range;
2: 
3: // TODO: make all of these configurable
4: // Maximum number of items in the outstanding queue
5: const MAX_OUTSTANDINGS: usize = 1024;
6: // Maximum value for the exponentially weighted moving average of the rate
7: const MAX_EWMA_RATE: u64 = u64::MAX / 2;
8: // Maximum rr_cursor value before it wraps to 0 (inclusive)
9: const MAX_RR_CURSOR: usize = 1024 * 1024;
10: // Maximum number of rounds for the round-robin assignment
11: const MAX_ROUNDS: usize = 1024 * 1024;
12: // This is the max number of accounts that can be registered in the system
13: const MAX_ACCOUNTS: usize = 4096;
14: // This is the max number of orders per account
15: const MAX_ORDERS_PER_ACCOUNT: usize = 128;
16: // This is the max number of items that can be queued to be settled in a single sweep
17: const MAX_QUEUE_SIZE: usize = 1024;
18: // This is the size of the order book (number of orders at a single price)
19: const MAX_ORDER_BOOK_SIZE: usize = 128;
20: // This is the max total number of orders
21: const MAX_ORDERS: usize = MAX_ACCOUNTS * MAX_ORDERS_PER_ACCOUNT;
22: // This is the max number of accounts per queue item
23: const MAX_ACCOUNTS_PER_ITEM: usize = 128;
24: 
25: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
26: pub enum OrderSide {
27:     Buy,
28:     Sell,
29: }
30: 
31: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
32: pub enum OrderStatus {
33:     Open,
34:     Cancelled,
35: }
36: 
37: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
38: pub enum EngineError {
39:     InvalidAccountId,
40:     InvalidOrderId,
41:     InvalidPrice,
42:     InvalidAmount,
43:     Overflow,
44:     DivisionByZero,
45:     MarketNotOpen,
46:     MarketNotClosed,
47:     ZeroBalance,
48:     ZeroAmount,
49:     AccountAlreadyRegistered,
50:     InsufficientBalance,
51:     MaxOrdersReached,
52:     MaxAccountsReached,
53:     MaxOutstandingsReached,
54:     MaxQueueSizeReached,
55: }
56: 
57: #[derive(Debug, Clone, Copy, PartialEq, Eq)]
58: pub enum MarketStatus {
59:     Open,
60:     Closed,
61: }
62: 
63: #[derive(Debug, Clone, Copy)]
64: pub struct EngineState {
65:     /// The number of accounts registered in the system.
66:     pub num_accounts: usize,
67:     /// The global balance of the engine.
68:     pub balance: u64,
69:     /// The round-robin cursor for assigning validators to items in the outstanding queue.
70:     pub rr_cursor: usize,
71:     /// The sweep generation counter.
72:     pub sweep_generation: u64,
73:     /// Total consumption is the amount of tokens that have been consumed by the sweep
74:     pub consumption: u64,
75:     /// The status of the market.
76:     pub market_status: MarketStatus,
77: }
78: 
79: #[derive(Debug, Clone, Copy)]
80: pub struct OrderInfo {
81:     pub price: u64,
82:     pub amount: u64,
83:     pub side: OrderSide,
84:     pub status: OrderStatus,
85: }
86: 
87: #[derive(Debug, Clone, Copy)]
88: pub struct AccountInfo {
89:     /// balance is the amount of tokens that are available to be traded.
90:     pub balance: u64,
91:     /// order_count is the number of orders that have been placed.
92:     pub order_count: usize,
93:     /// withdrawn is a flag that indicates if the account has been withdrawn.
94:     pub withdrawn: bool,
95:     /// withdraw_balance is the amount of tokens that have been withdrawn from the account
96:     pub withdraw_balance: u64,
97: }
98: 
99: impl Default for AccountInfo {
100:     fn default() -> Self {
101:         AccountInfo {
102:             balance: 0,
103:             order_count: 0,
104:             withdrawn: false,
105:             withdraw_balance: 0,
106:         }
107:     }
108: }
109: 
110: #[derive(Debug, Clone, Copy)]
111: pub struct OutstandingItem {
112:     pub account_id: usize,
113:     pub round: usize,
114:     pub amount: usize,
115:     pub accounts: [usize; MAX_ACCOUNTS_PER_ITEM],
116:     pub num_accounts: usize,
117: }
118: 
119: impl Default for OutstandingItem {
120:     fn default() -> Self {
121:         OutstandingItem {
122:             account_id: 0,
123:             round: 0,
124:             amount: 0,
125:             accounts: [0; MAX_ACCOUNTS_PER_ITEM],
126:             num_accounts: 0,
127:         }
128:     }
129: }
130: 
131: #[derive(Debug, Clone, Copy)]
132: pub struct QueueItem {
133:     pub account_id: usize,
134:     pub price: u64,
135:     pub amount: u64,
136:     pub accounts: [usize; MAX_ACCOUNTS_PER_ITEM],
137:     pub num_accounts: usize,
138: }
139: 
140: impl Default for QueueItem {
141:     fn default() -> Self {
142:         QueueItem {
143:             account_id: 0,
144:             price: 0,
145:             amount: 0,
146:             accounts: [0; MAX_ACCOUNTS_PER_ITEM],
147:             num_accounts: 0,
148:         }
149:     }
150: }
151: 
152: #[derive(Debug, Clone)]
153: pub struct Engine {
154:     pub state: EngineState,
155:     pub accounts: Vec<AccountInfo>,
156:     pub orders: Vec<OrderInfo>,
157:     pub outstanding: Vec<OutstandingItem>,
158:     pub queue: Vec<QueueItem>,
159:     pub order_book: Vec<Vec<usize>>,
160: }
161: 
162: impl Engine {
163:     pub fn new(state: EngineState) -> Self {
164:         Engine {
165:             state,
166:             accounts: vec![AccountInfo::default(); MAX_ACCOUNTS],
167:             orders: vec![OrderInfo { price: 0, amount: 0, side: OrderSide::Buy, status: OrderStatus::Open }; MAX_ORDERS],
168:             outstanding: vec![OutstandingItem::default(); MAX_OUTSTANDINGS],
169:             queue: vec![QueueItem::default(); MAX_QUEUE_SIZE],
170:             order_book: vec![vec![]; MAX_ORDER_BOOK_SIZE],
171:         }
172:     }
173: }
174: 
175: impl Engine {
176:     pub fn register_account(&mut self, account_id: usize) -> Result<(), EngineError> {
177:         if self.state.market_status != MarketStatus::Open {
178:             return Err(EngineError::MarketNotOpen);
179:         }
180:         if account_id >= MAX_ACCOUNTS {
181:             return Err(EngineError::InvalidAccountId);
182:         }
183:         if self.accounts[account_id].balance > 0 || self.accounts[account_id].order_count > 0 {
184:             return Err(EngineError::AccountAlreadyRegistered);
185:         }
186:         self.state.num_accounts += 1;
187:         Ok(())
188:     }
189: }
190: 
191: impl Engine {
192:     pub fn deposit(&mut self, account_id: usize, amount: u64) -> Result<(), EngineError> {
193:         if self.state.market_status