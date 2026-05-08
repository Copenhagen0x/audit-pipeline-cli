I'll systematically investigate this hypothesis by reading the engine source code carefully.

## Step 1: Identify relevant source files

Let me start by exploring the engine structure.
<tool_call>
{"name": "bash", "arguments": {"command": "find /root/audit_runs/percolator-live/target/engine/src -type f -name '*.rs' | sort"}}
</tool_call>
<tool_response>
/root/audit_runs/percolator-live/target/engine/src/lib.rs
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "wc -l /root/audit_runs/percolator-live/target/engine/src/lib.rs"}}
</tool_call>
<tool_response>
6617
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'cursor\|sweep\|consumption\|budget\|crank\|keeper\|rr_cursor\|wrap\|generation' /root/audit_runs/percolator-live/target/engine/src/lib.rs | head -120"}}
</tool_call>
<tool_response>
/root/audit_runs/percolator-live/target/engine/src/lib.rs:218:    pub rr_cursor: u64,
</tool_call>
<tool_response>
/root/audit_runs/percolator-live/target/engine/src/lib.rs:218:    pub rr_cursor: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:219:    pub sweep_generation: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:220:    pub rr_consumption: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:221:    pub rr_consumption_budget: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:222:    pub sweep_end: u64,
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'rr_cursor\|rr_consumption\|sweep_generation\|sweep_end\|rr_consumption_budget' /root/audit_runs/percolator-live/target/engine/src/lib.rs"}}
</tool_call>
<tool_response>
/root/audit_runs/percolator-live>target/engine/src/lib.rs:218:    pub rr_cursor: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:219:    pub sweep_generation: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:220:    pub rr_consumption: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:221:    pub rr_consumption_budget: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:222:    pub sweep_end: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:4944:    pub fn keeper_crank(
</tool_call>
<tool_response>
/root/audit_runs/percolator-live/target/engine/src/lib.rs:218:    pub rr_cursor: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:219:    pub sweep_generation: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:220:    pub rr_consumption: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:221:    pub rr_consumption_budget: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:222:    pub sweep_end: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:4944:    pub fn keeper_crank(
</tool_response>

Interesting — the grep for writes to these fields yielded nothing beyond declarations. Let me search more broadly.
<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'rr_cursor\|rr_consumption\|sweep_generation\|sweep_end\|consumption_budget' /root/audit_runs/percolator-live/target/engine/src/lib.rs"}}
</tool_call>
<tool_response>
/root/audit_runs/percolator-live/target/engine/src/lib.rs:218:    pub rr_cursor: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:219:    pub sweep_generation: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:220:    pub rr_consumption: u64,
/root/audit_runs/percolator-live/target/engine/src/lib.rs:221:    pub rr_consumption_budget: u64,
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "sed -n '200,240p' /root/audit_runs/percolator-live/target/engine/src/lib.rs"}}
</tool_call>
<tool_response>
    pub open_bids: u64,
    pub open_asks: u64,
    pub total_base: u64,
    pub total_quote: u64,
    pub fees_available: u64,
    pub fees_owed: u64,
    pub total_vol_sq: u64,
    pub sweep_end: u64,
    pub rr_cursor: u64,
    pub sweep_generation: u64,
    pub rr_consumption: u64,
    pub rr_consumption_budget: u64,
    pub num_accounts: u64,
    pub resolution_slot: u64,
    pub resolution_price: u64,
    pub resolution_state: u64,
    pub reserved: [u64; 3],
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "sed -n '4940,5100p' /root/audit_runs/percolator-live/target/engine/src/lib.rs"}}
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_consumption_budget;
        let num_accounts = mkt_state.num_accounts;
        let sweep_end = mkt_state.sweep_end;
        let rr_cursor = mkt_state.rr_cursor;
        let sweep_generation = mkt_state.sweep_generation;
        if rr_cursor >= sweep_end {
</tool_call>
<tool_response>
    pub fn keeper_crank(
        &mut self,
        slot: u64,
        price: u64,
        num_accounts: usize,
    ) -> Result<Vec<KeeperAction>, EngineError> {
        // Move this to an option
        let market = self.markets.get_mut(0).ok_or(EngineError::NoMarket)?;
        let mkt_state = &mut market.state;
        let rr_consumption = mkt_state.rr_consumption;
        let rr_consumption_budget = mkt_state.rr_