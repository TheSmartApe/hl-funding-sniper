"""Rich terminal UI — colored, structured, real-time output.

Everything visual goes through this module. The console is a singleton so
panels, tables, and progress bars all share the same output stream.

The file log (logs/bot.log) stays plain text for grep / parsing.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .config import Config
from .data_client import MarketSnapshot


# ─── Singleton console ────────────────────────────────────────────────────
# legacy_windows=False forces the modern (ANSI) renderer even on Windows.
# Without it, rich falls back to the cp1252 Win32 console API which can't
# encode emojis like 📊 / ❤️ → UnicodeEncodeError. Combined with the UTF-8
# stdout reconfigure in main.py bootstrap, this gives a fully Unicode-safe
# terminal on every shell (cmd, PowerShell, Git Bash, Linux).
console = Console(highlight=False, log_path=False, legacy_windows=False)


# ─── Color theme ──────────────────────────────────────────────────────────
C_OK = "bright_green"
C_WARN = "yellow"
C_ERR = "bright_red"
C_INFO = "cyan"
C_DIM = "dim"
C_HEADER = "bold magenta"
C_ENTRY = "bold bright_green"
C_EXIT_WIN = "bold bright_green"
C_EXIT_LOSS = "bold bright_red"
C_HALT = "bold bright_red"


# ─── Boot banner ──────────────────────────────────────────────────────────


def print_banner(cfg: Config) -> None:
    """Startup banner with full config summary."""
    if not cfg.execution.dry_run and not getattr(cfg.execution, "observe_only", False):
        mode = "[bold bright_red]🔴 LIVE[/]"
    elif getattr(cfg.execution, "observe_only", False):
        mode = "[bold bright_cyan]👁  OBSERVE-ONLY[/]"
    else:
        mode = "[bold yellow]🟡 DRY-RUN[/]"

    tls = "[red]TLS DISABLED[/]" if not cfg.execution.tls_verify else "[green]TLS ok[/]"

    body = Text.from_markup(
        f"[dim]Mode:[/]        {mode}\n"
        f"[dim]Network:[/]     {cfg.hyperliquid.network}    {tls}\n"
        f"[dim]Capital:[/]     [bold]${cfg.account.capital_usdc:,.2f}[/]\n"
        f"[dim]Caps:[/]        max {cfg.account.max_concurrent_positions} pos × "
        f"{cfg.account.max_position_pct}% = {cfg.account.max_total_exposure_pct}% total exposure\n"
        f"[dim]Universe:[/]    OI ≥ ${cfg.universe.min_open_interest_usd/1e6:.0f}M, "
        f"spread ≤ {cfg.universe.max_spread_bps:.1f} bps\n"
        f"[dim]Entry:[/]       funding ≥ [green]{cfg.entry.min_funding_apr_pct}%[/] APR, "
        f"persist [cyan]{cfg.entry.persistence_hours}h[/], "
        f"|z| ≥ [cyan]{cfg.entry.min_funding_zscore}[/]\n"
        f"[dim]Exit:[/]        TP [green]{cfg.exit.take_profit_pct}%[/] | "
        f"SL [red]{cfg.exit.stop_loss_pct}%[/] | "
        f"funding < [yellow]{cfg.exit.funding_apr_exit_threshold}%[/] | "
        f"|z| < {cfg.exit.exit_on_zscore_below} | "
        f"timeout {cfg.exit.timeout_hours}h\n"
        f"[dim]Sizing:[/]      majors {cfg.sizing.leverage_majors}x, "
        f"midcaps {cfg.sizing.leverage_midcaps}x\n"
        f"[dim]Tick every:[/] {cfg.scheduler.tick_interval_seconds}s "
        f"({cfg.scheduler.tick_interval_seconds // 60} min)"
    )
    console.print(
        Panel(
            body,
            title="[bold]🎯  HL FUNDING SPIKE SNIPER  🎯[/]",
            title_align="center",
            border_style=C_HEADER,
            padding=(1, 2),
        )
    )


# ─── Tick lifecycle ───────────────────────────────────────────────────────


def print_tick_start(tick_num: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    console.print()
    console.print(
        Rule(
            f"[bold cyan]⏵  Tick #{tick_num}  ·  {now}[/]",
            style="cyan",
        )
    )


def print_tick_end(duration_s: float) -> None:
    console.print(
        Rule(
            f"[dim]⏹  Tick end  ·  {duration_s:.1f}s[/]",
            style="dim",
        )
    )


def print_sleeping(seconds: int) -> None:
    mins = seconds // 60
    secs = seconds % 60
    console.print(
        f"[{C_DIM}]💤  Sleeping {mins}m {secs}s until next tick...[/]"
    )


# ─── Live monitor (1s refresh between ticks) ──────────────────────────────


def _compute_sl_tp_prices(direction: str, entry_px: float, cfg: Config) -> tuple:
    """Return (sl_price, tp_price) given the position direction and entry."""
    sl_pct = cfg.exit.stop_loss_pct / 100
    tp_pct = cfg.exit.take_profit_pct / 100
    if direction == "short":
        return entry_px * (1 + sl_pct), entry_px * (1 - tp_pct)
    return entry_px * (1 - sl_pct), entry_px * (1 + tp_pct)


def make_live_positions_renderable(
    rows: list,
    mids: dict,
    cfg: Config,
    capital: float,
    next_tick_in_s: int,
):
    """Build the renderable shown by the live monitor. Returns a Group of widgets.

    `mids` is the dict returned by HL all_mids() — {coin: mid_price_string}.
    """
    header = Text.from_markup(
        f"[bold cyan]📊  LIVE POSITIONS MONITOR[/]  "
        f"[dim]· Capital ${capital:,.2f} · Next tick in[/] "
        f"[bold yellow]{next_tick_in_s // 60}m {next_tick_in_s % 60:02d}s[/] "
        f"[dim]· refresh 1s[/]"
    )

    if not rows:
        body = Text.from_markup(
            f"\n[{C_DIM}]No open positions. "
            f"Bot scans every {cfg.scheduler.tick_interval_seconds // 60} min "
            f"for new signals.[/]\n"
        )
        return Panel(Group(header, body), border_style=C_DIM, padding=(0, 1))

    t = Table(
        show_header=True, header_style="bold", box=None, padding=(0, 1),
        expand=True,
    )
    t.add_column("ID", justify="right", style=C_DIM)
    t.add_column("Coin", style=C_INFO, no_wrap=True)
    t.add_column("Side", no_wrap=True)
    t.add_column("Size", justify="right")
    t.add_column("Entry", justify="right")
    t.add_column("Mark", justify="right")
    t.add_column("SL @ (Δ%)", justify="right")
    t.add_column("TP @ (Δ%)", justify="right")
    t.add_column("Unr. PnL", justify="right")
    t.add_column("Unr. %", justify="right")
    t.add_column("Funding $", justify="right")

    total_pnl = 0.0
    total_funding = 0.0

    for r in rows:
        coin = r["coin"]
        mid_str = mids.get(coin)
        try:
            mark = float(mid_str) if mid_str is not None else 0.0
        except (TypeError, ValueError):
            mark = 0.0
        entry = float(r["entry_price"])
        size_usd = float(r["size_usd"])
        direction = r["direction"]
        sign = -1 if direction == "short" else 1

        sl_px, tp_px = _compute_sl_tp_prices(direction, entry, cfg)

        # Distances in % from current mark (signed: positive means mark below SL/TP)
        sl_dist = (sl_px - mark) / mark * 100 if mark > 0 else 0
        tp_dist = (tp_px - mark) / mark * 100 if mark > 0 else 0

        unr = sign * (mark - entry) / entry * size_usd if entry > 0 and mark > 0 else 0
        unr_pct = sign * (mark - entry) / entry * 100 if entry > 0 and mark > 0 else 0
        fund = float(r["funding_collected_usd"] or 0)

        total_pnl += unr
        total_funding += fund

        unr_color = C_OK if unr >= 0 else C_ERR
        fund_color = C_OK if fund >= 0 else C_ERR
        side_color = "bold bright_red" if direction == "short" else "bold bright_green"

        t.add_row(
            str(r["id"]),
            coin,
            f"[{side_color}]{direction.upper()}[/]",
            f"${size_usd:,.2f}",
            f"{entry:,.4f}",
            f"{mark:,.4f}" if mark else "—",
            f"[red]{sl_px:,.4f}[/] [dim]({sl_dist:+.1f}%)[/]",
            f"[green]{tp_px:,.4f}[/] [dim]({tp_dist:+.1f}%)[/]",
            f"[{unr_color}]{unr:+,.4f}$[/]",
            f"[{unr_color}]{unr_pct:+.2f}%[/]",
            f"[{fund_color}]{fund:+,.4f}$[/]",
        )

    total = total_pnl + total_funding
    total_col = C_OK if total >= 0 else C_ERR
    footer = Text.from_markup(
        f"[dim]Total unrealized:[/] [{C_OK if total_pnl >= 0 else C_ERR}]"
        f"{total_pnl:+,.4f}$[/]   "
        f"[dim]Total funding:[/] [{C_OK if total_funding >= 0 else C_ERR}]"
        f"{total_funding:+,.4f}$[/]   "
        f"[dim]→ Combined:[/] [{total_col}]{total:+,.4f}$[/]"
    )

    return Panel(
        Group(header, t, footer),
        border_style=C_INFO,
        padding=(0, 1),
    )


def live_monitor_loop(
    rows_provider,
    mids_provider,
    cfg: Config,
    capital_provider,
    duration_seconds: int,
    stop_flag,
) -> None:
    """Run a 1Hz live-updating positions panel for `duration_seconds`.

    Providers are callables (no args) so the loop can fetch fresh data each tick:
      rows_provider()    -> list of position rows from DB
      mids_provider()    -> dict {coin: mid_price_str} from HL all_mids
      capital_provider() -> float current capital USD
    stop_flag is a dict-like with key "flag" — when True, the loop exits.
    """
    import time as _time
    start = _time.time()

    def _build():
        try:
            rows = rows_provider()
        except Exception:
            rows = []
        try:
            mids = mids_provider()
        except Exception:
            mids = {}
        try:
            cap = capital_provider()
        except Exception:
            cap = 0.0
        remaining = max(0, int(duration_seconds - (_time.time() - start)))
        return make_live_positions_renderable(rows, mids, cfg, cap, remaining)

    with Live(_build(), refresh_per_second=2, console=console, transient=False) as live:
        while not stop_flag.get("flag") and (_time.time() - start) < duration_seconds:
            _time.sleep(1)
            try:
                live.update(_build())
            except Exception:
                # never let UI errors break the loop
                pass


# ─── Progress helpers ─────────────────────────────────────────────────────


@contextmanager
def step_spinner(label: str) -> Iterator[None]:
    """Spinner for a single step (snapshot, etc.)."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        transient=True,
        console=console,
    ) as p:
        p.add_task(label, total=None)
        yield


def iter_with_progress(items: List, label: str) -> Iterator:
    """Yield items with a progress bar showing N/total and ETA."""
    if not items:
        return iter([])
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        transient=True,
        console=console,
    ) as p:
        task = p.add_task(label, total=len(items))
        for item in items:
            yield item
            p.advance(task)


# ─── Eligible / open tables ───────────────────────────────────────────────


def print_eligible_top(
    eligibles: List[MarketSnapshot], top: int = 10
) -> None:
    """Show top N eligibles ranked by |funding| absolute."""
    if not eligibles:
        console.print(f"[{C_DIM}]No eligible coins after filters.[/]")
        return
    sorted_e = sorted(
        eligibles, key=lambda s: abs(s.funding_apr_pct), reverse=True
    )[:top]
    t = Table(
        title=f"Top {len(sorted_e)} eligible / {len(eligibles)} total (by |funding|)",
        title_style=C_HEADER,
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
    )
    t.add_column("Coin", style=C_INFO, no_wrap=True)
    t.add_column("Funding %", justify="right")
    t.add_column("OI", justify="right")
    t.add_column("Spread bps", justify="right")
    t.add_column("Premium bps", justify="right")
    t.add_column("Mark", justify="right")
    for s in sorted_e:
        fcol = (
            C_OK if s.funding_apr_pct > 0
            else C_ERR if s.funding_apr_pct < 0
            else "white"
        )
        pcol = (
            C_OK if s.premium_bps > 0
            else C_ERR if s.premium_bps < 0
            else "white"
        )
        t.add_row(
            s.coin,
            f"[{fcol}]{s.funding_apr_pct:+.2f}[/]",
            f"${s.open_interest_usd/1e6:,.1f}M",
            f"{s.spread_bps:.1f}" if s.spread_bps else "—",
            f"[{pcol}]{s.premium_bps:+.0f}[/]",
            f"{s.mark_px:,.4f}",
        )
    console.print(t)


def print_candidates_table(
    candidates: list, cfg: Config, top: int = 15,
) -> None:
    """Show per-coin diagnostic table: which entry gate passes/fails.

    `candidates` is a list of (MarketSnapshot, EntryDecision) tuples.
    Sorted by |funding| descending so the most interesting come first.
    """
    if not candidates:
        console.print(f"[{C_DIM}]No eligible coins after universe filter.[/]")
        return

    sorted_c = sorted(
        candidates, key=lambda x: abs(x[0].funding_apr_pct), reverse=True
    )[:top]

    e = cfg.entry
    title = (
        f"🎯  Top {len(sorted_c)} eligible / {len(candidates)} total "
        f"[dim](need: funding ≥ {e.min_funding_apr_pct}% · "
        f"persist {e.persistence_hours}h · |z| ≥ {e.min_funding_zscore} · "
        f"|premium| ≤ {e.max_premium_bps}bps)[/]"
    )

    t = Table(
        title=title, title_style=C_HEADER, show_header=True, header_style="bold",
        box=None, padding=(0, 1),
    )
    t.add_column("Coin", style=C_INFO, no_wrap=True)
    t.add_column("Funding %", justify="right")
    t.add_column("Z-score", justify="right")
    t.add_column("Persist", justify="center")
    t.add_column("Premium bps", justify="right")
    t.add_column("OI", justify="right")
    t.add_column("Spread", justify="right")
    t.add_column("Status", no_wrap=True)

    def _gate(val: bool) -> str:
        return "[green]✓[/]" if val else "[red]✗[/]"

    for snap, d in sorted_c:
        # Funding cell with pass/fail color
        fcol = C_OK if d.funding_ok else C_ERR
        funding_cell = f"[{fcol}]{snap.funding_apr_pct:+.2f}[/] {_gate(d.funding_ok)}"

        # Z-score cell
        zcol = C_OK if d.zscore_ok else C_ERR
        z_cell = f"[{zcol}]{d.zscore:+.2f}[/] {_gate(d.zscore_ok)}"

        # Persistence: "X/N ✓" or "X/N ✗"
        pcol = C_OK if d.persistence_ok else C_ERR
        persist_cell = (
            f"[{pcol}]{d.persistence_count}/{d.persistence_total}[/] "
            f"{_gate(d.persistence_ok)}"
        )

        # Premium cell
        pmcol = C_OK if d.premium_ok else C_ERR
        prem_cell = f"[{pmcol}]{snap.premium_bps:+.0f}[/] {_gate(d.premium_ok)}"

        # OI cell
        oi_cell = f"${snap.open_interest_usd/1e6:,.1f}M"

        # Spread cell
        spread_cell = f"{snap.spread_bps:.1f}" if snap.spread_bps else "—"

        # Status: ✓ ENTRY or short reason
        if d.enter:
            status_cell = "[bold bright_green]✓ ENTRY[/]"
        else:
            # Short label of which gate blocked
            if not d.funding_ok:
                lbl = "funding low"
            elif not d.direction_ok:
                lbl = "wrong direction"
            elif not d.premium_ok:
                lbl = "premium extreme"
            elif not d.persistence_ok:
                lbl = f"persist {d.persistence_count}/{d.persistence_total}"
            elif not d.zscore_ok:
                lbl = f"|z|={abs(d.zscore):.2f} too low"
            else:
                lbl = "blocked"
            status_cell = f"[dim red]✗ {lbl}[/]"

        t.add_row(
            snap.coin, funding_cell, z_cell, persist_cell, prem_cell,
            oi_cell, spread_cell, status_cell,
        )

    console.print(t)


def print_open_positions(rows: list, snap_by_coin: dict) -> None:
    """Show currently open positions with live unrealized PnL."""
    if not rows:
        console.print(f"[{C_DIM}]📊  No open positions[/]")
        return
    t = Table(
        title=f"📊  Open positions ({len(rows)})",
        title_style=C_HEADER,
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
    )
    t.add_column("ID", justify="right", style=C_DIM)
    t.add_column("Coin", style=C_INFO)
    t.add_column("Side")
    t.add_column("Size", justify="right")
    t.add_column("Lev", justify="right")
    t.add_column("Entry", justify="right")
    t.add_column("Mark", justify="right")
    t.add_column("Unrealized", justify="right")
    t.add_column("Funding $", justify="right")
    for r in rows:
        snap = snap_by_coin.get(r["coin"])
        mark = snap.mark_px if snap else 0.0
        sign = -1 if r["direction"] == "short" else 1
        unr = (
            sign * (mark - r["entry_price"]) / r["entry_price"] * r["size_usd"]
            if r["entry_price"] else 0.0
        )
        unr_color = C_OK if unr >= 0 else C_ERR
        fund = r["funding_collected_usd"] or 0
        fund_color = C_OK if fund >= 0 else C_ERR
        side_style = "bold bright_red" if r["direction"] == "short" else "bold bright_green"
        t.add_row(
            str(r["id"]),
            r["coin"],
            f"[{side_style}]{r['direction'].upper()}[/]",
            f"${r['size_usd']:,.0f}",
            f"{r['leverage']}x",
            f"{r['entry_price']:,.4f}",
            f"{mark:,.4f}" if mark else "—",
            f"[{unr_color}]{unr:+,.2f}$[/]",
            f"[{fund_color}]{fund:+,.4f}$[/]",
        )
    console.print(t)


# ─── Event panels ─────────────────────────────────────────────────────────


def print_entry(
    coin: str,
    notional: float,
    leverage: int,
    entry_price: float,
    funding_apr: float,
    zscore: float,
    reason: str,
    mode_tag: str = "DRY-RUN",
) -> None:
    """Big green panel for a new entry."""
    body = Text.from_markup(
        f"[dim]Side:[/]         [bold bright_red]SHORT[/]\n"
        f"[dim]Notional:[/]     [bold]${notional:,.2f}[/]\n"
        f"[dim]Leverage:[/]     [bold]{leverage}x[/]\n"
        f"[dim]Entry price:[/]  {entry_price:,.6f}\n"
        f"[dim]Funding APR:[/]  [green]{funding_apr:+.2f}%[/]\n"
        f"[dim]Z-score:[/]      [cyan]{zscore:+.2f}[/]\n"
        f"[dim]Reason:[/]       {reason}"
    )
    console.print(
        Panel(
            body,
            title=f"🟢  ENTRY  ·  [bold]{coin}[/]  ·  [yellow]{mode_tag}[/]",
            title_align="left",
            border_style=C_ENTRY,
            padding=(1, 2),
        )
    )


def print_exit(
    coin: str,
    reason: str,
    price_pnl: float,
    funding: float,
    mode_tag: str = "DRY-RUN",
) -> None:
    """Big panel for an exit, green or red depending on total PnL."""
    total = price_pnl + funding
    if total >= 0:
        emoji, style = "🟢", C_EXIT_WIN
    else:
        emoji, style = "🔴", C_EXIT_LOSS
    price_col = C_OK if price_pnl >= 0 else C_ERR
    fund_col = C_OK if funding >= 0 else C_ERR
    body = Text.from_markup(
        f"[dim]Reason:[/]              [bold]{reason}[/]\n"
        f"[dim]Price PnL:[/]           [{price_col}]${price_pnl:+,.2f}[/]\n"
        f"[dim]Funding collected:[/]   [{fund_col}]${funding:+,.4f}[/]\n"
        f"[dim]Total:[/]               [{style}]${total:+,.2f}[/]"
    )
    console.print(
        Panel(
            body,
            title=f"{emoji}  EXIT  ·  [bold]{coin}[/]  ·  [yellow]{mode_tag}[/]",
            title_align="left",
            border_style=style,
            padding=(1, 2),
        )
    )


def print_potential_entry(
    coin: str,
    notional: float,
    leverage: int,
    entry_price: float,
    funding_apr: float,
    zscore: float,
    reason: str,
) -> None:
    """OBSERVE-ONLY: would have entered. Very visible block."""
    body = Text.from_markup(
        f"[bold yellow]⚠  This order WOULD have been placed (observe-only mode)[/]\n\n"
        f"[dim]Side:[/]         [bold bright_red]SHORT[/]\n"
        f"[dim]Notional:[/]     [bold]${notional:,.2f}[/]\n"
        f"[dim]Leverage:[/]     [bold]{leverage}x[/]\n"
        f"[dim]Entry price:[/]  {entry_price:,.6f}\n"
        f"[dim]Funding APR:[/]  [green]{funding_apr:+.2f}%[/]\n"
        f"[dim]Z-score:[/]      [cyan]{zscore:+.2f}[/]\n"
        f"[dim]Reason:[/]       {reason}"
    )
    console.print(
        Panel(
            body,
            title=f"👁   POTENTIAL ENTRY  ·  [bold]{coin}[/]",
            title_align="left",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


def print_potential_exit(
    coin: str, reason: str, est_price_pnl: float, est_funding: float,
) -> None:
    """OBSERVE-ONLY: would have closed."""
    body = Text.from_markup(
        f"[bold yellow]⚠  This close WOULD have been executed (observe-only mode)[/]\n\n"
        f"[dim]Reason:[/]              [bold]{reason}[/]\n"
        f"[dim]Est. price PnL:[/]      ${est_price_pnl:+,.2f}\n"
        f"[dim]Est. funding:[/]        ${est_funding:+,.4f}\n"
        f"[dim]Est. total:[/]          ${est_price_pnl + est_funding:+,.2f}"
    )
    console.print(
        Panel(
            body,
            title=f"👁   POTENTIAL EXIT  ·  [bold]{coin}[/]",
            title_align="left",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


# ─── Heartbeat & status ───────────────────────────────────────────────────


def print_heartbeat(
    n_eligible: int,
    n_open: int,
    n_signals: int,
    capital: float,
    halted: bool,
    halt_reason: Optional[str] = None,
) -> None:
    status_emoji = "🛑" if halted else "✅"
    status_text = "HALTED" if halted else "OK"
    color = C_HALT if halted else C_OK

    body = Text.from_markup(
        f"[dim]Eligible coins:[/]   [bold]{n_eligible}[/]\n"
        f"[dim]Open positions:[/]   [bold]{n_open}[/]\n"
        f"[dim]New signals:[/]      [bold]{n_signals}[/]\n"
        f"[dim]Capital:[/]          [bold]${capital:,.2f}[/]\n"
        f"[dim]Status:[/]           [{color}]{status_emoji} {status_text}[/]"
        + (f"\n[dim]Halt reason:[/]      [red]{halt_reason}[/]" if halt_reason else "")
    )
    console.print(
        Panel(
            body,
            title="❤️   HEARTBEAT",
            title_align="left",
            border_style=color,
            padding=(0, 2),
        )
    )


def print_halt(reason: str) -> None:
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]Reason:[/] [red]{reason}[/]\n\n"
                f"[dim]New entries are blocked. Open positions continue to be managed.[/]\n"
                f"[dim]Call risk.reset_halt() manually after reviewing.[/]"
            ),
            title="🛑  CIRCUIT BREAKER TRIPPED",
            title_align="left",
            border_style=C_HALT,
            padding=(1, 2),
        )
    )


# ─── Inline messages ──────────────────────────────────────────────────────


def info(msg: str) -> None:
    console.print(f"[{C_INFO}]ℹ  {msg}[/]")


def ok(msg: str) -> None:
    console.print(f"[{C_OK}]✓  {msg}[/]")


def warn(msg: str) -> None:
    console.print(f"[{C_WARN}]⚠  {msg}[/]")


def err(msg: str) -> None:
    console.print(f"[{C_ERR}]✗  {msg}[/]")


def dim(msg: str) -> None:
    console.print(f"[{C_DIM}]{msg}[/]")


def no_signals(min_apr: float, min_z: float) -> None:
    console.print(
        f"[{C_DIM}]ℹ  No new signals "
        f"(need funding ≥ {min_apr}% APR and |z| ≥ {min_z})[/]"
    )
