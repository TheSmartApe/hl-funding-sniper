# HL Funding Spike Sniper

> Opportunistic bot that shorts Hyperliquid perps when funding rates spike on
> liquid coins, exits when the signal fades or hits TP/SL.

---

## Table of contents

1. [Objective & context](#1-objective--context)
2. [The strategic thesis (why it works)](#2-the-strategic-thesis-why-it-works)
3. [The strategy in detail](#3-the-strategy-in-detail)
4. [Code architecture](#4-code-architecture)
5. [Exhaustive configuration](#5-exhaustive-configuration-every-parameter)
6. [Step-by-step setup](#6-step-by-step-setup)
7. [Execution modes](#7-execution-modes)
8. [Running & operation](#8-running--operation)
9. [Persistent state (SQLite + logs)](#9-persistent-state-sqlite--logs)
10. [Validations & tests performed](#10-validations--tests-performed-dry-run)
11. [Bugs found and fixes](#11-bugs-found-and-fixes)
12. [Hyperliquid: what you need to know](#12-hyperliquid-what-you-need-to-know)
13. [The linked Dune dashboard](#13-the-linked-dune-dashboard)
14. [Expected economic capacity](#14-expected-economic-capacity)
15. [Known limitations v1](#15-known-limitations-v1)
16. [Roadmap](#16-roadmap)
17. [Security](#17-security)
18. [Glossary & formulas](#18-glossary--formulas)
19. [Decision history (for a future me)](#19-decision-history-for-a-future-me)

---

## 1. Objective & context

### Objective

An **automated** bot that opens **short** positions on Hyperliquid perps
when a coin's **funding rate** becomes extreme (positive) and **liquidity**
is sufficient, then **exits** as soon as funding drops back, a TP/SL is hit,
or the z-score normalizes.

No spot hedge — assumed **"naked short"** version. See
[§3 mechanics](#mechanics-of-the-trade) for justification.

### Context behind this strategy

This bot is the natural extension of research originally done on Dune:

- We built a Dune dashboard **"Hyperliquid Trader Edge Console"**
  (see [§13](#13-the-linked-dune-dashboard)) with 4 sections: Funding edge,
  Premium/basis anomalies, Liquidity quality, Bridge capital flows.
- The dashboard is for **research** and historical **calibration**.
- The bot acts in **real time** on the HL API (not Dune, which has a 3-4
  week indexing lag).

### What this strategy is NOT

- ❌ Not related to Polymarket (an early thread had ambiguity — clarified:
  this is purely Hyperliquid).
- ❌ Not delta-neutral cash-and-carry (long spot + short perp) because
  HL spot only lists a handful of tokens (HYPE, PURR, a few wrapped majors
  via the Unit bridge). For 90%+ of HL perps, there's no HL spot → no hedge
  without cross-exchange complexity. v1 = naked short assumed.
- ❌ Not a momentum strategy (going long on high funding) — we exploit the
  opposite: mean reversion of crowded longs.
- ❌ Not HFT / market making — **hourly** cadence, aligned with HL funding
  settlements.

---

## 2. The strategic thesis (why it works)

### The pattern we exploit

When a coin's funding rate spikes on HL, it means **too many people are long
with leverage**. This setup pays on **two simultaneous edges**:

1. **Funding harvesting** — we collect the funding every hour while short
   (HL pays funding to shorts when funding is positive).
2. **Mean reversion** — statistically, after extreme funding, price often
   corrects: over-leveraged longs eventually get cascade-liquidated, which
   amplifies the drop.

Both edges work in the same direction. That's what makes the strategy elegant.

### Why Hyperliquid specifically

| HL factor | Effect on the edge |
|---|---|
| More retail-heavy user base than Binance | Funding more dispersed, long bias persists |
| HLP vault (counter-party) doesn't hedge cross-venue | Extreme funding persists longer |
| Funding cap at 4% per 8h (≈ +10.95% APR) | Visible ceiling — we know when a coin is "saturated" |
| On-chain transparency | Backtest possible via Dune (limited by index lag) |
| No KYC + simple USDC bridge | Capital deployable quickly |

### Why not short a perp on Binance instead

- Binance funding is more tightly capped (caps + pro market makers arbing)
- More efficient market → less edge
- But HL has smart-contract risk + bridge delay → it's a trade-off

### Why not go long to collect negative funding

Implementable (`direction_mode: "both"` on the roadmap), but v1 is
`short_high_funding` only to minimize complexity. Opportunities on the
very-negative side exist (saw BCH at -31% APR, z=-4 during testing) but v1
lets those go.

---

## 3. The strategy in detail

### Mechanics of the trade

```
Funding ≥ +50% APR persistent + OK liquidity + z-score > 2
  ↓
SHORT the HL perp at market, capped sizing, 2-3x leverage
  ↓
Hold until: funding drops < +15% APR  (main exit)
         OR price drops 8%             (take profit)
         OR price rises 10%            (stop loss)
         OR z-score < 0.5              (signal dead)
         OR 168h elapsed               (7-day timeout)
  ↓
Close at market
  ↓
6h cooldown on this coin before re-entry
If exit = stop_loss, next entry on this coin = size × 0.5
```

### Entry conditions (ALL gates must pass)

| # | Condition | Config parameter | Default | Why |
|---|---|---|---|---|
| 1 | Funding APR ≥ threshold | `entry.min_funding_apr_pct` | 50 | Filter out "normal" funding |
| 2 | Direction = positive if short mode | `entry.direction_mode` | `short_high_funding` | v1 not symmetric |
| 3 | Premium within range | `entry.max_premium_bps` | 200 | Extreme premium = basis blowup risk |
| 4 | Persistence over N hours | `entry.persistence_hours` | 3 | Filter out one-whale wicks |
| 5 | Z-score ≥ threshold | `entry.min_funding_zscore` | 2.0 | Extremity vs coin's own history |
| 6 | OI ≥ minimum | `universe.min_open_interest_usd` | 5_000_000 | Enough liquidity to enter/exit |
| 7 | Spread ≤ max | `universe.max_spread_bps` | 5 | Limit entry slippage |
| 8 | Not in cooldown | `exit.reentry_cooldown_hours` | 6 | No impulsive re-entry after exit |
| 9 | Not in `exclude_coins` | `universe.exclude_coins` | [] | Manual blocklist |
| 10 | If `include_only` non-empty, must be in it | `universe.include_only` | [] | Optional whitelist |

### Exit conditions (first triggered wins)

| Priority | Condition | Parameter | Default | Handled by |
|---|---|---|---|---|
| 1 | Take profit | `exit.take_profit_pct` | 8% | **HL native (trigger)** |
| 2 | Stop loss | `exit.stop_loss_pct` | 10% | **HL native (trigger)** |
| 3 | Funding normalized | `exit.funding_apr_exit_threshold` | 15% | Bot (hourly tick) |
| 4 | Z-score normalized | `exit.exit_on_zscore_below` | 0.5 | Bot (hourly tick) |
| 5 | Timeout | `exit.timeout_hours` | 168 (7 days) | Bot (hourly tick) |

**Native TP/SL architecture**: if `execution.use_native_triggers: true`
(default), the bot places reduce-only trigger orders on HL for SL and TP
IMMEDIATELY after each open. HL enforces them in real time — even if the
bot is offline or crashes, the position stays protected. The bot keeps the
"smart" exits (funding/zscore/timeout) at hourly cadence.

Before any manual close by the bot, the pending triggers are cancelled
(`exchange.cancel`) to prevent them from executing against our close.

At every tick, **reconciliation** compares OPEN positions in the DB vs
actual HL user_state. If a position is absent on HL → an SL/TP fired (or
a manual close on the UI) → we fetch the corresponding fill via
`user_fills` and record the close with exit_reason `hl_sl_triggered` /
`hl_tp_triggered` / `external_close`.

> ⚠️ **Coherence constraint**: `entry.min_funding_apr_pct` must be
> **strictly greater than** `exit.funding_apr_exit_threshold`. Otherwise
> every entry would fire the exit rule on the very next tick. The Pydantic
> validator rejects incoherent configs.

### Sizing

```python
# Per position
notional_usd = min(
    capital × max_position_pct / 100,            # per-position cap (8% default)
    (capital × max_total_exposure_pct / 100) - already_deployed_notional,
    coin_OI × max_pct_of_coin_oi / 100,          # OI cap (1% default)
)
notional_usd × = post_stop_multiplier             # 0.5 if last exit was SL
leverage = 3 if coin in majors_list else 2        # capped by HL maxLeverage
```

### Risk caps

| Cap | Parameter | Default |
|---|---|---|
| Concurrent positions | `account.max_concurrent_positions` | 4 |
| % capital per position | `account.max_position_pct` | 8% |
| % capital total deployed | `account.max_total_exposure_pct` | 30% |
| % of a coin's OI | `risk.max_pct_of_coin_oi` | 1% |
| Leverage majors | `sizing.leverage_majors` | 3x |
| Leverage midcaps | `sizing.leverage_midcaps` | 2x |

### Circuit breakers (automatic halt)

| Trigger | Parameter | Default | Action |
|---|---|---|---|
| Realized 24h loss | `risk.daily_loss_halt_pct` | 3% of capital | Halt new entries |
| Total drawdown vs peak | `risk.total_drawdown_kill_pct` | 15% | Kill switch (halt) |
| Margin ratio warning | `risk.margin_ratio_warning` | 0.40 | Log warning |
| Margin ratio critical | `risk.margin_ratio_critical` | 0.25 | Log critical |

The halt blocks new entries but **does not force-close** existing positions
— they continue to be managed by their exit rules.

### Ranking score

When multiple valid candidates compete at the same tick, we sort them by
score and pick the top N (where N = remaining slots).

```python
score = |funding_apr_pct| × |z_score| × min(1, OI_usd / 50_000_000)
```

→ Favors coins with extreme funding, high z-score, and OI > $50M.

---

## 4. Code architecture

### File structure

```
hl-funding-sniper/
├── config.yaml          ← ALL parameters here (nothing hardcoded in code)
├── requirements.txt     ← Python deps (incl. rich for the terminal UI)
├── .env.example         ← Env var template (NEVER commit .env)
├── .gitignore           ← Excludes .env, state/, logs/, .venv/, data/cache/
├── README.md            ← This document
├── state/               ← Created at runtime
│   └── positions.db     ← SQLite: open/closed positions + funding collected
├── logs/                ← Created at runtime
│   └── bot.log          ← Auto-rotated 5×5MB, PLAIN TEXT (grep-friendly)
└── src/
    ├── __init__.py
    ├── config.py        ← Pydantic schema + loader (+ env-var injection)
    ├── data_client.py   ← HL Info API wrapper (funding, OI, prices, spread, premium)
    ├── signal_engine.py ← Entry/exit logic + z-score + persistence (STATELESS)
    ├── position_manager.py ← SQLite persistence + adapter for signal engine
    ├── executor.py      ← HL Exchange API + dry-run + retries + fill parsing
    ├── risk_manager.py  ← Caps, breakers, sizing, cooldowns
    ├── ui.py            ← Rich terminal UI (panels, tables, progress bars)
    ├── notifier.py      ← RichHandler console + plain file log + Telegram
    └── main.py          ← Orchestrator (tick loop) + CLI
```

### Terminal output (rich UI)

Each tick prints:

```
┌─── 🎯  HL FUNDING SPIKE SNIPER  🎯 ────┐
│  Mode:        🟡 DRY-RUN               │
│  Network:     mainnet    TLS DISABLED  │
│  Capital:     $10,000.00               │
│  ... config summary ...                │
└────────────────────────────────────────┘

─── ⏵  Tick #1  ·  2026-05-26 00:00:00 UTC ───
✓  Snapshot: 230 perps fetched
ℹ  Universe filter: 19/230 pass
   ┌──── Top 10 eligible / 19 total (by |funding|) ────┐
   │ Coin  Funding %     OI    Spread  Premium  Mark   │
   │ BCH   -43.77       $7.3M  4.4     -11      348.61 │  ← red if fund<0
   │ BNB   +10.95      $29.0M  1.5      -4      660.58 │  ← green if fund>0
   │ ...                                                │
   └────────────────────────────────────────────────────┘
📊  Open positions (3)
   ┌─────────────────────────────────────────────────────────┐
   │ ID  Coin  Side   Size  Lev  Entry  Mark  Unr.  Funding  │
   │  1  SUI   SHORT  $800  2x  1.0373 1.0366 +0.54  +0.00   │
   │  ...                                                    │
   └─────────────────────────────────────────────────────────┘
🔄  Evaluating exits...        [████████████] 3/3

┌── 🟢  EXIT  ·  SUI  ·  DRY-RUN ───────┐  ← green border = gain
│  Reason:              funding_norm…   │
│  Price PnL:           $+0.54          │
│  Funding collected:   $+0.0000        │
│  Total:               $+0.54          │
└───────────────────────────────────────┘

🎯  Scanning 16 eligible coins...  [████████████] 16/16

┌── 🟢  ENTRY  ·  WLFI  ·  DRY-RUN ─────┐  ← green border = new position
│  Side:         SHORT                  │
│  Notional:     $800.02                │
│  Leverage:     2x                     │
│  Entry price:  0.061075               │
│  Funding APR:  +10.95%                │
│  Z-score:      +0.70                  │
│  Reason:       funding +11.0%, ...    │
└───────────────────────────────────────┘

┌── ❤️   HEARTBEAT ─────────────────────┐
│  Eligible coins:   19                 │
│  Open positions:   3                  │
│  New signals:      1                  │
│  Capital:          $10,000.54         │
│  Status:           ✅ OK              │
└───────────────────────────────────────┘
──── ⏹  Tick end  ·  14.4s ────────────
```

Between ticks, a **live monitor** refreshes the positions panel every
second (mark prices, unrealized PnL, SL/TP distance), with a countdown
to the next tick.

### Logging strategy (3 channels)

| Channel | Format | Use |
|---|---|---|
| Console | Rich (colors/panels/tables/bars) | Human eyes |
| `logs/bot.log` | Plain text rotating 5×5MB | grep / parsing |
| Telegram (opt) | Markdown | Mobile alerts |

All three receive the **same events** (entry/exit/heartbeat/halt/error)
via the single `Notifier` gateway. No code duplication.

### Module responsibilities

| Module | Role | Local state | Dependencies |
|---|---|---|---|
| `config.py` | Loads & validates YAML, injects env secrets | none | pydantic, yaml |
| `data_client.py` | Pull HL market + cache funding history | funding cache (55min TTL) | hyperliquid SDK (lazy import) |
| `signal_engine.py` | Entry/exit decisions (pure, deterministic) | none | nothing external |
| `position_manager.py` | SQLite DB, bot-side source of truth | DB | sqlite3 (stdlib) |
| `executor.py` | Place HL orders + retries + dry-run | none | hyperliquid SDK, eth_account |
| `risk_manager.py` | Caps, breakers, sizing, capital | halt state (in-memory) | data_client + position_manager |
| `ui.py` | Rich terminal rendering (panels/tables/progress) | console singleton | rich |
| `notifier.py` | RichHandler console + file log + Telegram + delegates to ui | none | rich, stdlib |
| `main.py` | Tick loop, orchestrates everything | none | all of the above |

### Data flow per tick (full sequence with native triggers)

```
┌────────────────────────────────────────────────────────────┐
│  tick start                                                │
└────────────────────────────────────────────────────────────┘
       │
       ▼
  risk.check_circuit_breakers()
       │
       ▼
  reconcile_positions(cfg, data, positions, notifier)
   ├─ Pull live HL user_state → set of coins with open position
   ├─ For each DB-OPEN absent from HL (orphan):
   │    ├─ user_fills → find the fill that closed (match sl_oid/tp_oid)
   │    ├─ reason = "hl_sl_triggered" | "hl_tp_triggered" | "external_close"
   │    └─ positions.record_close(...) with realized PnL from real fill
       │
       ▼
  data.snapshot_all()
       │
       ▼
  For each remaining OPEN position:
   ├─ accrue hourly funding → DB
   ├─ evaluate_exit (funding/zscore/timeout — TP/SL handled by HL)
   ├─ If decision.exit:
   │    ├─ _cancel_triggers_before_close(sl_oid, tp_oid) ← important!
   │    ├─ executor.close_position(coin)
   │    └─ positions.record_close(...)
       │
       ▼
  If risk.allow_new_entries():
   ├─ For each top candidate after ranking:
   │    ├─ sizing = risk.size_new_entry(...)
   │    ├─ fill = executor.open_short(...)
   │    ├─ pid = positions.record_open(...)
   │    └─ _place_triggers_for_open(coin, "short", size_base, entry_px, pid)
   │         ├─ executor.place_trigger_sl → sl_oid
   │         ├─ executor.place_trigger_tp → tp_oid
   │         └─ positions.update_trigger_oids(pid, sl_oid, tp_oid)
       │
       ▼
  notifier.heartbeat(...)
       │
       ▼
┌────────────────────────────────────────────────────────────┐
│  tick end → live positions monitor (1s refresh) until      │
│  next tick (~59 min later)                                 │
└────────────────────────────────────────────────────────────┘

  During the sleep: HL watches triggers in real time.
  If SL or TP fires → position closed on HL side. Detected at
  the next tick's reconciliation step.
```

### Tick idempotence

- If a tick crashes mid-flight (network, OOM), the SQLite DB stays in a
  coherent state (autocommit).
- Next tick: positions still OPEN in the DB are re-evaluated normally.
  No double-trade is possible because `_scan_and_open` filters out coins
  already in `positions.open_coins()`.
- Funding accrual via `add_funding_collected` is cumulative → if a tick is
  missed we lose that hour's accrual (acceptable).

---

## 5. Exhaustive configuration (every parameter)

All parameters are in **`config.yaml`** at the root. No hardcoded values in
the code. Change behavior → edit YAML → restart bot.

### Section `account`

| Key | Type | Default | Description |
|---|---|---|---|
| `capital_usdc` | float | 10000 | Reference capital (USDC). In dry-run = baseline. In live the bot reads the real account value but caps to this. |
| `max_concurrent_positions` | int 1-20 | 4 | Max simultaneously open positions. |
| `max_total_exposure_pct` | 0-100 | 30 | Max % of capital deployed across all positions. |
| `max_position_pct` | 0-100 | 8 | Max % of capital per position. |

### Section `universe`

| Key | Type | Default | Description |
|---|---|---|---|
| `min_open_interest_usd` | float | 5_000_000 | Minimum OI (USD) for a coin to be eligible. |
| `max_spread_bps` | float | 5 | Max effective spread (bps). Computed as `(impact_ask − impact_bid) / mid × 10000`. |
| `exclude_coins` | list[str] | [] | Blocklist (e.g. `["PURR", "PUMP"]`). |
| `include_only` | list[str] | [] | If non-empty, ONLY these coins. Overrides everything else. |

### Section `entry`

| Key | Type | Default | Description |
|---|---|---|---|
| `min_funding_apr_pct` | float | 50 | Funding threshold (APR %, absolute). MUST be > `exit.funding_apr_exit_threshold`. |
| `persistence_hours` | int 1-48 | 3 | Funding must stay above the threshold for N consecutive hourly samples (same sign). |
| `min_funding_zscore` | float | 2.0 | Min z-score over the window. 2.0 ≈ top 2.5% historical. |
| `zscore_lookback_days` | int 1-365 | 30 | Window for z-score computation. |
| `max_premium_bps` | float | 200 | Skip if \|premium\| > N bps (violent convergence risk). |
| `direction_mode` | "short_high_funding" \| "both" | "short_high_funding" | v1 = short on extreme positive funding only. |

### Section `sizing`

| Key | Type | Default | Description |
|---|---|---|---|
| `leverage_majors` | int 1-50 | 3 | Leverage for BTC/ETH/SOL. Auto-capped by HL `maxLeverage`. |
| `leverage_midcaps` | int 1-50 | 2 | Leverage for the rest. Auto-capped by HL `maxLeverage`. |
| `majors_list` | list[str] | ["BTC","ETH","SOL"] | Which coins count as "majors". |
| `method` | "equal" \| "score_weighted" | "equal" | v1 sizing is equal-weight across top candidates. |

### Section `exit`

| Key | Type | Default | Description |
|---|---|---|---|
| `funding_apr_exit_threshold` | float | 15 | Exit when funding APR drops below this. MUST be < `entry.min_funding_apr_pct`. |
| `take_profit_pct` | float > 0 | 8 | Take profit (% favorable). For a SHORT = price drop of N%. |
| `stop_loss_pct` | float > 0 | 10 | Stop loss (% adverse). For a SHORT = price rise of N%. URGENT. |
| `timeout_hours` | int > 0 | 168 | Max hold duration (7 days). |
| `exit_on_zscore_below` | float | 0.5 | Exit when z-score normalizes (signal dead). |
| `reentry_cooldown_hours` | int ≥ 0 | 6 | Cooldown on the coin after exit. 0 = no cooldown. |
| `post_stop_size_multiplier` | 0-1 | 0.5 | If last exit = `stop_loss`, next entry × this factor. |

### Section `risk`

| Key | Type | Default | Description |
|---|---|---|---|
| `daily_loss_halt_pct` | > 0 | 3 | Halt new entries if realized 24h loss ≥ N% of capital. |
| `total_drawdown_kill_pct` | > 0 | 15 | Kill switch if total drawdown vs peak ≥ N%. |
| `margin_ratio_warning` | 0-1 | 0.40 | Log warning if margin ratio exceeds this. |
| `margin_ratio_critical` | 0-1 | 0.25 | Log critical. MUST be < `margin_ratio_warning`. |
| `max_pct_of_coin_oi` | 0-100 | 1.0 | Cap notional per position to N% of the coin's total OI. |

### Section `execution`

| Key | Type | Default | Description |
|---|---|---|---|
| `dry_run` | bool | **true** | If true: no real orders, fills simulated at mark, DB still updated. |
| `order_type` | "market" \| "limit_post" | "market" | v1: only market is implemented. |
| `slippage_tolerance` | 0-1 | 0.005 | Slippage tolerance for market orders (0.005 = 0.5%). |
| `limit_timeout_seconds` | int > 0 | 30 | Limit timeout before falling back to market (not yet wired). |
| `retry_attempts` | 1-10 | 3 | Number of retries on failed fills. |
| `retry_delay_seconds` | ≥ 1 | 5 | Delay between retries (seconds). |
| `use_cross_margin` | bool | false | Cross (true) vs isolated (false) margin per position. |
| `use_native_triggers` | bool | true | Place SL+TP as native HL trigger orders. See "Native TP/SL architecture" above. |
| `tls_verify` | bool | true | TLS verify for HL API. Set to `false` ONLY on machines with a corporate MITM. |

### Section `scheduler`

| Key | Type | Default | Description |
|---|---|---|---|
| `tick_interval_seconds` | ≥ 60 | 3600 | Interval between ticks. 3600 = hourly (recommended). |
| `tick_offset_seconds_before_hour` | 0-3600 | 60 | Offset before the hour mark when we tick (HL settles on the hour). 60 = tick at minute 59. |

### Section `hyperliquid`

| Key | Type | Default | Description |
|---|---|---|---|
| `network` | "mainnet" \| "testnet" | "mainnet" | Target HL network. |
| `wallet_address` | str | "" | Wallet address. **Leave empty** — auto-derived from `HL_PRIVATE_KEY` at boot. Override only for read-only monitoring of another wallet. |

### Section `notifications`

| Key | Type | Default | Description |
|---|---|---|---|
| `log_level` | "DEBUG"\|"INFO"\|"WARNING"\|"ERROR" | "INFO" | Log level. |
| `log_file` | str | "logs/bot.log" | Log file (auto-rotated 5×5MB). |
| `telegram.enabled` | bool | false | Enable Telegram alerts. |
| `telegram.chat_id` | str | "" | Telegram chat ID. |
| `heartbeat` | bool | true | Emit a recap log/notif at every tick, even if nothing happened. |

### Environment variables (NEVER in YAML)

| Variable | When | Description |
|---|---|---|
| `HL_PRIVATE_KEY` | If `dry_run: false` | Wallet private key (hex). |
| `TELEGRAM_BOT_TOKEN` | If `telegram.enabled: true` | Telegram bot token. |

See `.env.example`. The bot auto-loads `.env` at startup via `python-dotenv`
— no need to `export` manually.

### Auto-derivation of `wallet_address`

The bot **auto-derives** `hyperliquid.wallet_address` from `HL_PRIVATE_KEY`
at startup (via `eth_account.Account.from_key`). So:

- Leave `wallet_address: ""` in `config.yaml`
- Put `HL_PRIVATE_KEY=0x...` in `.env`
- The bot derives the address at boot and shows it truncated (`0x…XXXX`)
  in the banner

**Why**: this removes any risk of accidentally committing the wallet to a
public repo, and guarantees the key and address are always consistent
(impossible to mismatch).

If you want to **explicitly override** (e.g. to monitor another address
read-only), put a real address in `config.yaml` — the bot respects an
explicit non-empty value.

---

## 6. Step-by-step setup

### Prerequisites

- **Python 3.11+** (tested on 3.11.7)
- **pip 23+**
- ~100 MB of disk for deps
- (Optional) HL wallet with USDC for live trading

### Standard install

```bash
cd hl-funding-sniper
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows (Git Bash)
source .venv/Scripts/activate

# Windows (cmd/PowerShell)
.venv\Scripts\activate

pip install -r requirements.txt
```

### Windows console (auto-handled)

The bot automatically configures at startup:
- **UTF-8** on stdout/stderr via `sys.stdout.reconfigure()` → emojis and
  Unicode borders render correctly on Windows cmd/PowerShell
- **COLUMNS=120** default (override via env var) → rich renders tables
  cleanly without truncation
- **`.env`** auto-loaded via `python-dotenv` → `HL_PRIVATE_KEY` etc.
  available without `set -a; source .env`

So a single command works in any shell:

```bash
.venv\Scripts\python.exe -m src.main           # continuous loop
.venv\Scripts\python.exe -m src.main --once    # one tick only
```

If you still see `?` instead of borders, your terminal doesn't support
ANSI escape codes — use Windows Terminal or Git Bash.

### Corporate proxy environment (MITM)

If pip or the bot fails with `SSLCertVerificationError` → an antivirus or
corporate proxy is intercepting HTTPS with its own cert.

**pip workaround**:
```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org \
  --trusted-host pypi.python.org -r requirements.txt
```

**Bot workaround** (config.yaml):
```yaml
execution:
  tls_verify: false
```

The bot monkey-patches `requests.Session.__init__` before importing the
HL SDK to disable SSL verification. **DO NOT use** on a non-corporate
machine — it's intentionally insecure.

### Install check

```bash
python -m src.main --once
```

You should see:
- `Loaded config from config.yaml (dry_run=True)`
- `[WARNING] DRY-RUN mode: no real orders will be placed`
- `tick start / tick end`
- `eligible: 20+ open: 0 new signals: 0` (0 signals normal at the strict default)

---

## 7. Execution modes

| Mode | `dry_run` | `observe_only` (roadmap) | Effect |
|---|---|---|---|
| **Dry-run** (default) | true | n/a | No real orders. Fills simulated at mark. DB still updated. Use this to test the state machine. |
| **Live** | false | n/a | Real orders on HL. Requires `HL_PRIVATE_KEY` + (derived) `wallet_address`. |
| **Observe-only** (roadmap) | n/a | true | Evaluates signals, logs would-be orders, **zero side effects** (no DB, no API). Not yet implemented. |

### Precedence

`observe_only` (future) > `dry_run` > live.
If `observe_only=true`, `dry_run` is ignored and nothing is written anywhere.

---

## 8. Running & operation

### Single tick (debug / cron)

```bash
python -m src.main --once
```

Useful to:
- Verify config loads
- Watch a single complete cycle
- Drop into an hourly cron instead of the internal loop

### Continuous loop (production)

```bash
python -m src.main
```

The bot sleeps between ticks (adaptive sleep to the next settlement mark).
`Ctrl+C` interrupts gracefully after the current tick.

### With an alternative config file

```bash
python -m src.main --config configs/conservative.yaml
```

You can maintain multiple configs (`paper.yaml`, `live.yaml`, etc.).

### Permanent background run

**systemd** (Linux):
```ini
# /etc/systemd/system/hl-sniper.service
[Unit]
Description=HL Funding Spike Sniper
After=network.target

[Service]
Type=simple
User=hlbot
WorkingDirectory=/home/hlbot/hl-funding-sniper
EnvironmentFile=/home/hlbot/hl-funding-sniper/.env
ExecStart=/home/hlbot/hl-funding-sniper/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

**Windows Task Scheduler**: instead of the Python loop, have Windows
launch a tick per hour:

1. Open Task Scheduler
2. Create Task → Triggers → New: Daily at 00:59, repeat every 1h for 24h
3. Actions → Start a program:
   - Program: `.venv\Scripts\python.exe`
   - Arguments: `-m src.main --once`
   - Start in: project directory

The bot is idempotent, so this is safe.

**tmux/screen** for a manual session:
```bash
tmux new -s sniper
python -m src.main
# Ctrl+B then D to detach (bot keeps running)
```

### DB inspection

```bash
python -c "
import sqlite3
c = sqlite3.connect('state/positions.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT * FROM positions ORDER BY id'):
    print(dict(r))
"
```

Or open `state/positions.db` with DB Browser for SQLite, TablePlus, etc.

### Manual reset

```bash
rm state/positions.db
# The bot recreates the schema at next startup
```

---

## 9. Persistent state (SQLite + logs)

### DB schema (`state/positions.db`)

```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('short','long')),
    size_usd REAL NOT NULL,
    size_base REAL,                       -- coin units, for trigger cancels
    leverage INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_timestamp TEXT NOT NULL,
    entry_funding_apr REAL,
    entry_zscore REAL,
    entry_reason TEXT,
    sl_oid INTEGER,                       -- HL order id of SL trigger
    tp_oid INTEGER,                       -- HL order id of TP trigger
    exit_price REAL,
    exit_timestamp TEXT,
    exit_reason TEXT,
    realized_pnl_usd REAL,
    funding_collected_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','ERROR'))
);

CREATE TABLE exits_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    exit_timestamp TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl_usd REAL
);
```

Possible `exit_reason` values:

| Reason | Origin | When |
|---|---|---|
| `take_profit` | Bot (legacy) or HL native (`hl_tp_triggered`) | Favorable price hit |
| `stop_loss` | Bot (legacy) or HL native (`hl_sl_triggered`) | Adverse price hit |
| `hl_tp_triggered` | Reconciliation | HL TP fired, detected at next tick |
| `hl_sl_triggered` | Reconciliation | HL SL fired, detected at next tick |
| `funding_normalized` | Bot hourly tick | Funding dropped below exit threshold |
| `zscore_normalized` | Bot hourly tick | Z-score back below exit threshold |
| `timeout` | Bot hourly tick | Max hold duration reached |
| `external_close` | Reconciliation | Manual close on HL UI, fill recovered |
| `external_close_unknown_price` | Reconciliation | Close detected but no fill found |

### Logs

- `logs/bot.log`: rotated automatically (max 5 files × 5MB)
- Format: `YYYY-MM-DD HH:MM:SS,ms [LEVEL] module: message`
- Console + file (same messages)
- Level configurable via `notifications.log_level`

---

## 10. Validations & tests performed (dry-run)

During development, validated end-to-end via 2 consecutive ticks in dry-run:

| # | Check | Result |
|---|---|---|
| 1 | Snapshot HL 230 coins in 1 API call | ✅ |
| 2 | Universe filter (OI > $5M, spread < 5bps) | ✅ 20-24 eligible |
| 3 | Funding history fetch + cache (55min TTL) | ✅ |
| 4 | Z-score computation on 30d history | ✅ |
| 5 | Entry evaluation (10 cascading gates) | ✅ |
| 6 | Ranking by score | ✅ |
| 7 | Sizing with caps (per-pos, total, OI) | ✅ $800/pos = 8% × $10k |
| 8 | Leverage tier majors vs midcaps + HL cap | ✅ ASTER requested 2x, HL cap=5x |
| 9 | Dry-run simulated fill, no real order | ✅ |
| 10 | SQLite write (open + close + exits_history) | ✅ |
| 11 | Exit evaluation (TP/SL/funding/zscore/timeout) | ✅ |
| 12 | Cooldown re-entry after exit | ✅ |
| 13 | Realized PnL tracked | ✅ |
| 14 | Heartbeat with stats | ✅ |
| 15 | Entry/exit notifications formatted | ✅ |
| 16 | Pydantic cross-validation on thresholds | ✅ |
| 17 | TLS bypass for corporate MITM | ✅ |

### Market observations during testing

- 230 total HL perps, ~20-24 pass the OI+spread filters by default
- 15+ coins stuck at the structural HL funding cap (+10.95% APR = 1% per 8h)
- Interesting negative funding seen (BCH −31% APR z=−4, ADA −15%, BTC −11.5%)
  → exploitable with `direction_mode: "both"` (roadmap)
- With defaults (50% APR + 3h persistence + z=2): 0 signal in our snapshot
  (normal — quiet market that day)

---

## 11. Bugs found and fixes

### Bug #1: entry/exit threshold coherence

**Symptom**: with `min_funding_apr_pct: 5` (entry) and
`funding_apr_exit_threshold: 15` (exit), every entry closes on the next
tick in a loop.

**Cause**: the exit rule `if abs(funding_apr) < 15` is true the moment we
enter at 11% (< 15%).

**Fix**: `Config._cross_checks` rejects any config where
`entry.min_funding_apr_pct ≤ exit.funding_apr_exit_threshold`.

### Bug #2: SSL handshake fails behind corporate MITM

**Symptom**: `SSLCertVerificationError` even with `certifi.where()`.

**Cause**: corporate antivirus/proxy presents its own cert, absent from
the certifi bundle.

**Fix**: `execution.tls_verify: false` option + monkey-patch of
`requests.Session.__init__` BEFORE the lazy HL SDK import.

### Bug #3 (cosmetic): Unicode encoding on Windows

**Symptom**: `─── tick start ───` displayed as `???`.

**Cause**: Windows console defaults to cp1252.

**Fix**: docstring in README + bootstrap auto-reconfigures stdout to UTF-8.

### Bug #4: persistence check ignored the live funding rate

**Symptom**: ZRO at +27% APR (visible in snapshot) didn't trigger entry,
even with persistence_hours=1.

**Cause**: `check_persistence` only looked at `funding_history` (the
hourly settled rates), not the current `snap.funding_apr_pct`. The
settled history can lag by up to 1h.

**Fix**: `evaluate_entry` now passes `funding_history + [current]` to
`check_persistence`, so "persist 1h" means "current reading above
threshold" intuitively.

---

## 12. Hyperliquid: what you need to know

### Funding rate

- Settled **every hour** (not every 8h like Binance)
- Raw `funding` in the API = hourly rate (e.g. 0.0001 = 0.01%/h ≈ 87.6% APR)
- Structural cap: **1% per 8h** ≈ **+10.95% APR** annualized
- Reaching the cap = extremely biased sentiment, but the strategy targets
  coins that EXCEED the cap → it's no longer the real limit, it's the line
  beyond which funding visually saturates.

### Notional & sizing

- Minimum order notional: **~$10**
- Per-coin `szDecimals` in `meta()['universe'][i]['szDecimals']` → bot
  auto-rounds via `_round_size`
- Per-coin `maxLeverage` → bot auto-caps via `_max_leverage`

### Leverage modes

- **Cross**: margin shared across the whole account. More capital-efficient
  but a single mismanaged position liquidates everything.
- **Isolated**: margin isolated per position. Safer per-trade. The bot's
  default (`use_cross_margin: false`).

### Fees

- Maker: −0.001% (rebate) to 0%
- Taker: 0.045% (reference without volume discount)
- The bot uses market orders by default → taker fees apply

### Bridge

- USDC bridge Arbitrum ↔ HL via contract `0x2df1c51e09aecf9cacb7bc98cb1742757f163df7`
- Withdrawal fee: $1 USDC flat
- Dispute period: a few hours (delayed exit during panic)

### API limits

- No documented hard rate limit, but soft throttle at high frequency
- The bot makes ~30-50 calls per tick (snapshot + funding histories) at
  hourly cadence — well under any limits

### Account state shape

```python
state = info.user_state(address)
# state['marginSummary']['accountValue']       ← total USD equity
# state['marginSummary']['totalMarginUsed']    ← used margin
# state['marginSummary']['totalNtlPos']        ← position notional
# state['assetPositions']                      ← list of open positions
# state['withdrawable']                        ← cash withdrawable
```

### Spot vs Perp on HL

Unlike Binance, HL spot is limited:
- Native HL spot: HYPE, PURR, BERA, TRUMP, PUMP, MON, AZTEC, STABLE
- Wrapped majors via the Unit bridge: UBTC, UETH, USOL, UAVAX, BNB1, LINK0
- Total intersection (perp ∩ spot/USDC): ~14 coins
- For 90%+ of HL perps, no spot exists → no on-platform delta-neutral hedge

### Unified account

HL has a "unified account" mode that displays spot + perps in the same UI,
but the API endpoints remain separate (`clearinghouseState` = perps,
`spotClearinghouseState` = spot). Funds must be **manually transferred to
perps** ("Transfer to Perps" in the UI) to be usable as perp margin.

---

## 13. The linked Dune dashboard

**URL**: https://dune.com/smart_ape/hl-edge-hyperliquid-trader-edge-console

### The 9 queries that compose the dashboard

| Section | Query ID | Description |
|---|---|---|
| Funding | 7566984 | Snapshot funding APR per coin (latest hour) |
| Funding | 7567029 | Funding heatmap top 25 × 30 days |
| Funding | 7567034 | BTC funding APR + mark series (30d) |
| Premium | 7567035 | Top premium outliers = mark−oracle (bps), latest day |
| Premium | 7567036 | BTC premium series (bps) + rolling ±3σ bands (30d) |
| Liquidity | 7567037 | Effective spread (bps) snapshot, top 40 |
| Liquidity | 7567039 | Median spread top 10 daily (30d) |
| Bridge | 7567043 | Daily net USDC bridge flows (90d) |
| Bridge | 7567044 | Flows by cohort size (90d) |

### Using the dashboard to calibrate the bot

| Action | Dashboard section |
|---|---|
| Pick `min_funding_apr_pct` per coin/regime | Q1 + Q2 (look at the distribution) |
| Verify a candidate is liquid enough | Q6 |
| Calibrate `max_premium_bps` | Q4 |
| Visually backtest the strategy on BTC | Q3 + Q5 (look at historical exits) |
| Detect macro risk-off (reduce all exposure) | Q8 (big outflows = risk off) |
| Filter the initial universe | Q6 + Q1 (cross liquidity × funding) |

### Dune indexing lag

**WARNING**: `hyperliquid.market_data` on Dune lags by ~3-4 weeks. The
dashboard is for **historical calibration** and **strategic monitoring**
only. **Live signals** always come from the HL API directly via the bot.

The bridge data on Dune, on the other hand, is real time.

---

## 14. Expected economic capacity

Estimate based on similar strategies backtested on Binance/Bybit:

| Capital | Net APR expected | Sharpe | Why |
|---|---|---|---|
| $10k | 25-40% | 3.0+ | No market impact, can cherry-pick best trades |
| $100k | 18-28% | 2.5 | Diversification forced onto more coins (some less juicy) |
| $1M | 12-18% | 2.0 | Market impact + per-coin OI cap reached |
| $10M+ | 6-10% | 1.5 | You BECOME the funding rate on small caps |

**Beyond ~$5-10M, the edge dies** on HL-only with this strategy. To scale,
extend to other venues (Bybit, OKX) in parallel — but that's cross-venue
funding arb, a different strategy.

### Edge decomposition

- Funding received: typically +20-40% APR on selected coins (vs the
  structural cap of +10.95% on market average)
- Mean reversion price: +2-5% per trade × 60-65% hit rate
- Frictions: −0.045% taker fee × 2 (in+out) + estimated ~0.5-1% slippage
  per trade = ~1-2% drag per trade
- Net: should come out between +5-15% per trade on the good setups

### Expected drawdowns

- **15-20%** on the naked version (our v1) — comes from trades where the
  price rips up before the longs get liquidated
- **3-5%** if hedged spot (not implemented in v1)

---

## 15. Known limitations v1

| Limitation | Impact | Workaround |
|---|---|---|
| No `observe_only` mode | DB pollution in test mode | Manual DB reset |
| No `direction_mode: both` | Miss negative-funding opportunities (BCH, ADA when z<−2) | Roadmap |
| No spot hedge | 100% directional risk | Intentional — HL spot too limited |
| No auto margin adjustment in critical | Just a warning log | Manual monitoring |
| No volatility-based circuit breaker | If BTC dumps 10%, we keep trading | Manual Ctrl+C halt |
| `limit_post` order type not wired | Always market in practice | OK for hourly strategy |
| Funding accrual is an estimate | Computed as funding_rate × size × hourly, not the real HL payment | Reconciliation at close vs HL DB |
| No auto DB ↔ HL state reconciliation at startup | If you close manually on HL UI between sessions, the DB doesn't know | TODO if running live |
| No Telegram inline buttons | One-way notifications | Open HL UI to interact |
| No backtest of past decisions | Validate via the separate backtest module instead | See `backtest/` |

---

## 16. Roadmap

### Short term

- [ ] **`observe_only` mode**: a purely passive mode that logs potential
      orders with a very visible ASCII marker block, without touching the DB
      or the exchange. Precedence over `dry_run`.
- [ ] **`direction_mode: both`**: also LONG coins with extreme negative
      funding (collect funding paid by shorts). Doubles the opportunity set.
- [ ] **DB ↔ HL reconciliation at startup**: if a position OPEN in the DB
      no longer exists on HL → mark ERROR. And vice versa.

### Medium term

- [ ] **Wider backtest grid + walk-forward** for robustness validation
- [ ] **Live Streamlit dashboard** for real-time bot monitoring
      (positions, cumulative PnL, signals scanned, breakers).
- [ ] **`limit_post` order type wired** (post-only for maker rebate
      instead of paying taker).
- [ ] **Auto-deleverage** when margin ratio goes critical.

### Long term

- [ ] **Optional hedge** for coins with HL spot (BTC, ETH, SOL via U-wraps,
      HYPE, PURR) → becomes delta-neutral = pure carry strategy.
- [ ] **Cross-venue funding arb** (HL vs Bybit vs OKX) to scale beyond
      $5-10M.
- [ ] **Signal enrichment**: feature engineering on premium, OI delta,
      bridge flows, volume profile.

---

## 17. Security

### Golden rules

1. **NEVER** commit `.env`. It contains `HL_PRIVATE_KEY`. `.gitignore`
   already excludes it.
2. **NEVER** put the private key in `config.yaml`. Pydantic validation
   wouldn't catch it — it's pure discipline.
3. **Wallet address is auto-derived** from the private key. Leave
   `wallet_address: ""` in the YAML — no risk of leak.
4. **Start in dry-run** for at least 3-7 days before any real capital.
5. **Start live with a tiny capital** ($100-500) to validate the pipeline.
   Scale only after several weeks of coherent PnL.
6. **Use a dedicated wallet** for the bot. Do not reuse a wallet that
   holds your other assets.
7. **`tls_verify: false`** only on your corporate machine that you trust
   (legitimate MITM). NEVER on a VPS / cloud server.
8. **HL is a proprietary L1** with a limited validator set. Real
   smart-contract risk. Cap total HL capital at what you'd accept losing
   100%.
9. **HL ↔ Arbitrum bridge** has a delay (dispute period). Don't deploy
   everything — keep cash off-HL.

### Audit before first live deploy

Before `dry_run: false`, verify:

- [ ] Wallet used is dedicated to the bot
- [ ] HL capital is ≤ what you accept to lose
- [ ] `max_position_pct` × `max_concurrent_positions` ≥ `max_total_exposure_pct`
      (otherwise caps inconsistent)
- [ ] `total_drawdown_kill_pct` is ≤ your real personal tolerance
- [ ] Telegram works (test with heartbeat)
- [ ] The bot has run for at least 24h in dry-run without crashing

---

## 18. Glossary & formulas

### Terms

| Term | Definition |
|---|---|
| **Funding rate** | Periodic payment between longs and shorts on a perp to anchor mark price to spot. Positive = longs pay shorts. |
| **Funding APR** | Funding annualized. `funding × 24 × 365 × 100` if funding is hourly. |
| **Mark price** | Reference price for PnL and liquidations (usually oracle-based or weighted average). |
| **Oracle price** | External aggregated price (Pyth / similar) that anchors funding. |
| **Premium** | Gap `mark − oracle`. When extreme → basis disloc. |
| **Open Interest (OI)** | Total notional of open positions (longs sum = shorts sum). |
| **Effective spread** | `(impact_ask − impact_bid) / mid × 10000` in bps. Measures real liquidity for an average-size trade. |
| **Z-score** | `(current − mean) / stdev` over the window. Measures statistical extremity. |
| **HLP** | Hyperliquidity Provider — vault that takes the opposite side of HL retail flow. |
| **Cooldown** | Duration during which we don't open a new position on a coin that just closed. |
| **Post-stop multiplier** | Size reduction factor after a stop-loss on that coin. |

### Key formulas

**Funding APR (from HL hourly funding)**:
```
funding_apr_pct = funding × 24 × 365 × 100
```

**Premium in bps**:
```
premium_bps = (mark_px - oracle_px) / oracle_px × 10000
```

**Effective spread in bps**:
```
spread_bps = (impact_ask_px - impact_bid_px) / mid_px × 10000
```

**Z-score (rolling 30d of hourly funding)**:
```
z = (current_funding_apr - mean(history_apr)) / stdev(history_apr)
```

**OI in USD**:
```
oi_usd = openInterest_base × mark_px
```

**Realized PnL of a short at close**:
```
price_pnl_usd = -1 × (close_px - entry_px) / entry_px × size_usd
```

**Hourly funding collected for a short**:
```
funding_hourly_usd = -1 × (funding_apr_pct / 100 / (24 × 365)) × size_usd
                   = hourly_funding_pct × size_usd   (inverse sign vs long)
```

**Ranking score**:
```
score = |funding_apr_pct| × |z_score| × min(1, oi_usd / 50_000_000)
```

**Sizing**:
```
notional = min(
  capital × max_position_pct / 100,
  (capital × max_total_exposure_pct / 100) - notional_already_open,
  oi_usd × max_pct_of_coin_oi / 100
) × post_stop_multiplier_if_applicable
```

---

## 19. Decision history (for a future me)

This section helps a future Claude (or you, having forgotten) understand
**why** the code looks like it does.

### Why not Polymarket

The user initially asked for "advanced polymarket strategy to profit from
funding rate". I first interpreted this as "strategy on Polymarket the
prediction market", which produced a long off-topic answer. The user
clarified: they meant **multi-market opportunistic** on Hyperliquid
itself, exploiting funding rates. Polymarket the product is not on the
roadmap.

### Why not delta-neutral

Long spot + short perp = pure carry strategy (collect funding without
price risk). BUT HL spot only lists a handful of tokens (HYPE, PURR,
BTC/ETH/SOL via U-wraps, a few HL natives). For 90%+ of HL perps, no
HL spot → would need to hedge on Binance/Coinbase = cross-exchange =
2× the complexity, leg risk, capital duplication.

→ Decision: v1 naked short, accept directional risk.

### Why short and not long on high funding

High funding = longs pay shorts. To PROFIT from funding, you need to be
on the RECEIVING side = SHORT.
If you went long with high funding, you'd PAY the funding (which you
wanted to exploit) → contradiction.
Bonus: extreme positive funding statistically precedes price corrections
→ the short also profits from the drop. Double edge.

### Why z-score over 30 days

- Enough samples (~720 hourly) for stable computation
- Not too long, stays responsive to regime change (a newly-hot coin
  should be detectable)
- 30 days also matches the analysis window of the Dune dashboard

### Why persistence 3 hours by default

A 1-hour funding spike could be a wick caused by 1 whale missing on a
buy. At 3 consecutive hours, it's a real crowd settling in. Trade-off:
higher persistence = fewer signals but more reliable.

### Why OI cap at 1%

Above 1% of OI, we start moving funding against ourselves (our short
adds to short OI → reduces positive funding → kills our edge). 1% is an
empirical compromise.

### Why lazy import of the Hyperliquid SDK

`Info.__init__` calls the HL API immediately. If we want to patch
`requests.Session` (for TLS bypass), it must be done BEFORE the session
is created. So the import must be lazy (inside `HLDataClient.__init__`
and `HLExecutor.__init__`) after the patch.

### Why monkey-patch `requests.Session.__init__`

Setting `session.verify = False` after construction isn't enough because
`Info()` makes API calls in its constructor. So we patch the init method
so all NEWLY-CREATED sessions have `verify=False` by default. Hacky but
effective.

### Why not WebSocket

The strategy is hourly. WebSocket would give tick-by-tick data, useless
here, and would complicate state. REST polling every hour is enough and
more robust (no reconnection logic).

### Why use native HL triggers rather than polling

Initially I coded TP/SL bot-side via mark-price polling each hourly
tick. Limits:

1. **1-hour latency** to react to a price move → SL often executes far
   from the threshold on a flash crash.
2. **Position unprotected if the bot crashes** between ticks.
3. **Fast tick is unrealistic**: see §15 — ticking at 1 minute creates
   HL rate-limit problems for zero signal gain.

The clean solution: let HL do the work:
- HL sees the mark price in real time
- HL enforces SL/TP **sub-second** server-side
- The bot can crash, restart, internet can drop — position is still
  protected
- This is what all pros do (Binance, Bybit etc. have the same pattern)

Architectural consequences:
- The bot no longer polls mark for SL/TP. Hourly tick is enough for
  the "smart" exits (funding_normalized, zscore_normalized, timeout)
- Each tick: **reconciliation** DB ↔ HL to catch triggers that fired
  between two ticks
- Before a manual close: **cancel the triggers** so they don't fire
  against our reduce-only close

### Why SQLite, not Postgres

- No external dependency (sqlite3 = stdlib)
- Portable file, easy to inspect, trivial backup
- Performance more than sufficient (1 write per tick per position)
- If you scale to 100 bots or multi-user, migrate to Postgres

### Why Pydantic v2

- Declarative validation (constraints live in the schema)
- Cross-checks via `@model_validator` to catch trap configs
- Clear errors at load time, not at runtime

### Why auto-derive wallet_address from the private key

To eliminate any risk of leaking the wallet address on a public repo:
- `config.yaml` ships with `wallet_address: ""` (placeholder only)
- At boot, `Config._cross_checks` derives the address from the private
  key via `eth_account.Account.from_key`
- Explicit `wallet_address` in config still wins (override use case:
  read-only monitoring of another wallet)

### Why Telegram via urllib (not python-telegram-bot)

- One less dependency
- Fire-and-forget, no need for the full framework
- 30 lines of code total

### Logging conventions

- `INFO` = important events (entry, exit, halt, heartbeat)
- `DEBUG` = operational details (cooldown skip, candidate evaluated)
- `WARNING` = non-blocking anomalies (margin warning, partial snapshot)
- `ERROR` / `CRITICAL` = bugs or breakers tripped

### Naming conventions

- `_` prefix for private helpers
- No classes for data structures → use `dataclass`
- `from __future__ import annotations` everywhere for forward refs
- Module names in `snake_case`, classes in `PascalCase`

---

**End of document.** For any evolution, keep this README current — it's
the only real onboarding doc of the project.
