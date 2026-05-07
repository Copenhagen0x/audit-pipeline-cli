# Audit Report: P4-funding-rate-mark-bias

**Hypothesis:** The funding rate captured by every instruction is computed BEFORE any `mark_ewma_e6` / `last_effective_price_e6` mutation in the same instruction, preventing same-tx self-bias.

---

## Step 1: Locate relevant source files

Let me read the repository structure and find the relevant files.

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
6572
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'mark_ewma_e6\|last_effective_price_e6\|funding_rate\|funding_rate_e6\|funding_cap' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | head -80"}}
</tool_call>
<tool_response>
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_price_e6: u64,
2:///         last_effective_power_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
2:///         mark_ewma_e6: u64,
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'mark_ewma_e6\|last_effective_price_e6\|funding_rate\|funding_rate_e6' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | grep -v '///' | head -100"}}
</tool_call>
<tool_response>
3239:    mark_ewma_e6: u64,
3253:    last_effective_price_e6: u64,
3267:    last_effective_power_e6: u64,
3275:    rr_cursor: u64,
3288:    rr_sweep_end: u64,
3295:    rr_sweep_generation: u64,
3327:    sweep_end: u64,
3349:    consumption: u64,
3361:    funding_rate_e9: i64,
3361:    funding_rate_e9: i64,
3375:    funding_rate_e9: i64,
3388:    funding_rate_e9: i64,
3388:    funding_rate_e9: i64,
3401:    funding_rate_e9: i64,
3401:    funding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_e6: i64,
3407:    nfunding_e6: i64,
3407:    nfunding_e6: i64,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
3407:    nfunding_e6: i50,
</tool_call>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "grep -n 'mark_ewma_e6\|last_effective_price_e6\|funding_rate_e9\|nfunding_rate_e9\|nfunding_e6' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | grep -v '///' | grep -v '^2:' | head -120"}}
</tool_call>
<tool_response>
3239:    mark_ewma_e6: u64,
3253:    last_effective_price_e6: u64,
3267:    last_effective_power_e6: u64,
3275:    rr_cursor: u64,
3288:    rr_sweep_end: u64,
3295:    rr_sweep_generation: u64,
3327:    sweep_end: u64,
3349:    consumption: u64,
3361:    funding_rate_e9: i64,
3375:    funding_rate_e9: i64,
3388:    funding_rate_e9: i64,
3401:    funding_rate_e9: i64,
3407:    nfunding_rate_e9: i64,
3407:    nfunding_e6: i64,
</tool_call>
</tool_response>

The grep output is showing structured data sections. Let me look at the actual code around those lines.
<tool_call>
{"name": "bash", "arguments": {"command": "sed -n '3200,3450n' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | cat"}}
</tool_call>
<tool_response>
sed: invalid option -- 'n'
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "sed -n '3200,3450p' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "awk 'NR>=3200 && NR<=3450' /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "head -3450 /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | tail -250"}}
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "wc -c /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
1076028
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | grep -c 'mark_ewma_e6'"}}
</tool_call>
<tool_response>
1
</tool_call>
<tool_response>
</tool_response>

The file has 1 line (it's a single long line) and 6572 newlines reported earlier was misleading. Let me check the actual structure.
<tool_call>
{"name": "bash", "arguments": {"command": "file /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs: Unicode text, UTF-8 text, with very long lines (65536), with CRLF line terminators
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs | tr '\r' '\n' | wc -l"}}
</tool_call>
<tool_response>
1
</tool_call>
<tool_response>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 -c \"
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
print('Total bytes:', len(data))
print('CR count:', data.count(b'\\r'))
print('LF count:', data.count(b'\\n'))
print('CRLF count:', data.count(b'\\r\\n'))
# First 500 bytes
print('First 200 chars:', data[:200])
\""}}
</tool_call>
<tool_response>
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076028
Total bytes: 1076away</s>
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
print('Total bytes:', len(data))
crlf = data.count(b'\r\n')
cr = data.count(b'\r') - crlf
lf = data.count(b'\n') - crlf
print('CRLF:', crlf, 'bare CR:', cr, 'bare LF:', lf)
print('First 300:', repr(data[:300]))
EOF"}}
</tool_call>
<tool_response>
Total bytes: 1076028
CRLF: 6572
bare CR: 0
bare LF: 0
First 300: b'/// # Percolator\r\n/// Percolator is a fully on-chain derivatives exchange. \r\n/// It matches bids and asks using a central limit order book (CLOB) matching engine. \r\n/// The matching engine is implemented as a Solana BPF program.\r\n/// \r\n/// # Key State\r\n/// \r\n/// ## Engine\r\n/// ```\r\n/// struct Engine {\r\n///     mark_ewma_e6: u64,\r\n///     ...\r\n/// ```\r\n/// \r\n/// ## Market Maker \r\n'
EOF
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
# Split on CRLF to get lines
lines = data.split(b'\r\n')
print('Number of lines:', len(lines))
# Search for mark_ewma_e6 in actual code (not doc comments)
for i, line in enumerate(lines, 1):
    if b'mark_ewma_e6' in line and b'///' not in line:
        print(f'Line {i}: {line.decode(errors="replace")}')
EOF"}}
</tool_call>
<tool_response>
Number of lines: 6572
For each non-finding (negative result):
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
print('Number of lines:', len(lines))
# Search for mark_ewma_e6 anywhere
for i, line in enumerate(lines, 1):
    if b'mark_ewma_e6' in line:
        print(f'Line {i}: {line.decode(errors="replace")[:120]}')
EOF"}}
</tool_call>
<tool_response>
Number of lines: 6572
Line 2: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
Line 11: ///         mark_ewma_e6: u64,
</tool_response>

The grep is returning repeated results for what appears to be line 2 and line 11 — this is a file with 6572 CRLF-separated lines. Let me search more carefully.
<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
print('Number of lines:', len(lines))
# Print lines 1-20
for i, line in enumerate(lines[:20], 1):
    print(f'Line {i}: {line.decode(errors="replace")[:100]}')
EOF"}}
</tool_call>
<tool_response>
Number of lines: 6572
Line 1: /// # Percolator
Line 2: /// Percolator is a fully on-chain derivatives exchange. 
Line 3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
Line 4: /// The matching engine is implemented as a Solana BPF program.
Line 5: /// 
Line 6: /// # Key State
Line 7: /// 
Line 8: /// ## Engine
Line 9: /// ```
Line 10: /// struct Engine {
Line 11: ///     mark_ewma_e6: u64,
Line 12: ///     last_effective_price_e6: u64,
Line 13: ///     last_effective_power_e6: u64,
Line 14: ///     rr_cursor: u64,
Line 15: ///     rr_sweep_end: u64,
Line 16: ///     rr_sweep_generation: u64,
Line 17: ///     ..
Line 18: /// ```
Line 19: /// 
Line 20: /// ## Market Maker
EOF
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
# Search for all occurrences of mark_ewma_e6 with actual line numbers
results = []
for i, line in enumerate(lines, 1):
    if b'mark_ewma_e6' in line:
        results.append((i, line.decode(errors='replace')[:120]))
print(f'Total occurrences: {len(results)}')
for lineno, text in results:
    print(f'Line {lineno}: {text}')
EOF"}}
</tool_call>
<tool_response>
Total occurrences: 18
Line 11: ///     mark_ewma_e6: u64,
Line 118: ///         mark_ewma_e6: u64,
Line 238: ///         mark_ewma_e6: u64,
Line 360: ///         mark_ewma_e6: u64,
Line 477: ///         mark_ewma_e6: u64,
Line 581: ///         mark_ewma_e6: u64,
Line 683: ///         mark_ewma_e6: u64,
Line 793: ///         mark_ewma_e6: u64,
Line 897: ///         mark_ewma_e6: u64,
Line 994: ///         mark_ewma_e6: u64,
Line 1089: ///         mark_ewma_e6: u64,
Line 1170: ///         mark_ewma_e6: u64,
Line 1256: ///         mark_ewma_e6: u64,
Line 1344: ///         mark_ewma_e6: u64,
Line 1423: ///         mark_ewma_e6: u64,
Line 1501: ///         mark_ewma_e6: u64,
Line 1578: ///         mark_ewma_e6: u64,
Line 1650: ///         mark_ewma_e6: u64,
EOF
</tool_response>

All 18 occurrences of `mark_ewma_e6` are in doc comments (`///`). Let me search for the actual implementation code.
<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
# Find lines that are NOT doc comments and contain relevant terms
terms = [b'mark_ewma', b'last_effective_price', b'funding_rate', b'fn ', b'struct Engine', b'pub fn']
for term in terms:
    count = sum(1 for l in lines if term in l and b'///' not in l)
    print(f'{term!r}: {count} non-doc occurrences')
EOF"}}
</tool_call>
<tool_response>
b'mark_ewma': 18 occurrences -> all doc
b'last_effective_price': multiple in doc only
b'funding_rate': multiple
b'fn ': many
b'struct Engine': some
b'pub fn': many
EOF
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')

terms = [b'mark_ewma', b'last_effective_price', b'funding_rate']
for term in terms:
    results = [(i, line.decode(errors='replace')[:120]) for i, line in enumerate(lines, 1) 
               if term in line and b'///' not in line]
    print(f'\n=== {term!r}: {len(results)} non-doc occurrences ===')
    for lineno, text in results[:20]:
        print(f'  Line {lineno}: {text}')
EOF"}}
</tool_call>
<tool_response>
=== b'mark_ewma': 0 non-doc occurrences ===

=== b'last_effective_price': 0 non-doc occurrences ===

=== b'funding_rate': 0 non-doc occurrences ===
EOF
</tool_response>

All field references are in doc comments only. This is a documentation-only file for the engine. Let me check if there's actual Rust implementation code elsewhere.
<tool_call>
{"name": "bash", "arguments": {"command": "find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/ -type f | sort"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap in tree
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/ -type f 2>/dev/null | sort -u"}}
</tool_call>
<tool_response>
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs
</tool_response>

Only two files present. Let me examine the Cargo.toml and look at the overall structure of lib.rs more carefully.
<tool_call>
{"name": "bash", "arguments": {"command": "cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/Cargo.toml"}}
</tool_call>
<tool_response>
[package]
name = "percolator"
version = "0.1.0"
edition = "2021"
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
print(f'Total lines: {len(lines)}')
# Show first non-doc-comment code lines
code_lines = [(i, line.decode(errors='replace')) for i, line in enumerate(lines, 1) if not line.startswith(b'///')]
print(f'Non-doc lines: {len(code_lines)}')
for lineno, text in code_lines[:30]:
    print(f'Line {lineno}: {text[:120]}')
EOF"}}
</tool_call>
<tool_response>
Total lines: 6572
Non-doc lines: 0
EOF
</tool_call>
<tool_response>
</tool_response>

Every single line in lib.rs is a doc comment (`///`). This file is a **specification document written entirely as doc comments** — it contains no executable Rust code. Let me now examine the content of those doc comments systematically to find the funding rate ordering claims.
<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
# Print lines 1-80 to see the overall doc structure
for i, line in enumerate(lines[:80], 1):
    print(f'{i:4}: {line.decode(errors="replace")}')
EOF"}}
</tool_call>
<tool_response>
   1: /// # Percolator
   2: /// Percolator is a fully on-chain derivatives exchange. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   4: /// The matching engine is implemented as a Solana BPF program.
   5: /// 
   6: /// # Key State
   7: /// 
   8: /// ## Engine
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
   9: /// ```
</tool_response>

<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
# Print lines 1-150
for i, line in enumerate(lines[:150], 1):
    decoded = line.decode(errors='replace')
    print(f'{i:4}: {decoded}')
EOF"}}
</tool_call>
<tool_response>
   1: /// # Percolator
   2: /// Percolator is a fully on-chain derivatives exchange. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
   3: /// It matches bids and asks using a central limit order book (CLOB) matching engine. 
</tool_response>

The output is looping — it looks like there's an issue with line number deduplication in the terminal output display. Let me try a different approach.
<tool_call>
{"name": "bash", "arguments": {"command": "python3 << 'EOF'
import sys
data = open('/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs', 'rb').read()
lines = data.split(b'\r\n')
# Write to stdout directly
out = []
for i, line in enumerate(lines[:150], 1):
    decoded = line.decode(errors='replace')
    out.append(f'{i:4}: {decoded}')
sys.stdout.write('\n'.join(out) + '\n')
sys.stdout.flush()
EOF"}}
</tool_call>
<tool_response>
   1: /// # Percolator
   2: /// Percolator is a fully on-chain derivatives exchange. 
   3: /// It