#!/usr/bin/env python3
"""
risk_manager.py - Deterministic Risk Manager, Sizing Engine, and Veto Filters.

Standard library only. Never places orders.
Takes candidate picks and returns approve-or-veto decisions with explicit reason codes.
"""

from dataclasses import dataclass, field
import datetime
import math
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

DB_PATH = os.environ.get("SIM_LOG_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_log.db"))

# Veto reason codes
APPROVED = "APPROVED"
STAT_UNVALIDATED = "STAT_UNVALIDATED"
GAME_STARTED = "GAME_STARTED"
STALE_PRICE = "STALE_PRICE"
NO_USABLE_PRICE = "NO_USABLE_PRICE"
PROB_OUT_OF_BOUNDS = "PROB_OUT_OF_BOUNDS"
EDGE_UNDER_MIN = "EDGE_TOO_SMALL"
EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
MODEL_GAP_TOO_WIDE = "MODEL_GAP_TOO_WIDE"
UNCONFIRMED_WITHIN_1H = "UNCONFIRMED_NEAR_TIP"
UNCONFIRMED_NEAR_TIP = "UNCONFIRMED_NEAR_TIP"
OPPOSING_SIDE_HELD = "OPPOSING_SIDE_HELD"
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
CAP_REACHED_BET = "CAP_REACHED_BET"
CAP_REACHED_PLAYER = "CAP_REACHED_PLAYER"
CAP_REACHED_GAME = "CAP_REACHED_GAME"
CAP_REACHED_DAY = "CAP_REACHED_DAY"
MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"


@dataclass
class RiskConfig:
    # Portfolio exposure caps (% of bankroll)
    cap_per_bet: float = 0.02          # 2% max per bet
    cap_per_player: float = 0.03       # 3% max per player (props share cap)
    cap_per_game: float = 0.05         # 5% max per game
    cap_per_day: float = 0.12          # 12% max per day
    max_open_positions: int = 12       # Concurrent open position limit
    daily_loss_limit_pct: float = 0.08 # 8% daily stop-loss limit

    # Probability & Edge Gates
    prob_min: float = 0.20             # Gated to [20%, 80%]
    prob_max: float = 0.80
    min_edge_after_fees: float = 0.05  # Edge under 5 points after fees vetoed
    max_market_gap: float = 0.15       # > 15pt model-market gap vetoed
    stale_price_seconds: float = 600.0 # 10 minutes max age for price snapshot

    # Sizing parameters
    kelly_fraction: float = 0.25       # Quarter Kelly
    shrink_weight: float = 0.50        # Shrink halfway toward market: p_sim*0.5 + p_mkt*0.5
    unconfirmed_size_mult: float = 0.50# Half-size stake if unconfirmed > 1h out
    fee_rate: float = 0.07             # Kalshi fee shape: 0.07 * P * (1 - P)

    # Validated Stats
    validated_stats: Tuple[str, ...] = ("PTS", "3PM", "3PT", "AST", "REB")
    paper_only_stats: Tuple[str, ...] = ("PRA",)

    # Live Mode Gate (Default strictly False / Paper-only)
    live_enabled: bool = False
    min_settled_props_for_live: int = 300


class Candidate:
    """Flexible Candidate class supporting both positional and keyword constructors."""
    def __init__(self, *args, **kwargs):
        if len(args) == 13:
            # Positional constructor from team_card.py / check_risk_manager.py:
            # (cid, player, game_id, stat, line, side, p_yes, bid, ask, ticker, confirmed, tipoff_ts, snap_ts)
            self.cand_id = str(args[0])
            self.cid = self.cand_id
            self.player = str(args[1])
            self.game_id = str(args[2])
            self.team = kwargs.get("team", "")
            self.opp = kwargs.get("opp", "")
            self.stat = str(args[3])
            self.line = float(args[4])
            self.side = str(args[5]).upper()
            self.p_yes = float(args[6])
            self.sim_prob = self.p_yes
            self.bid = args[7]
            self.yes_bid = args[7]
            self.ask = args[8]
            self.yes_ask = args[8]
            self.ticker = str(args[9])
            self.confirmed = bool(args[10])
            self.tipoff_ts = float(args[11]) if args[11] is not None else 0.0
            self.snap_ts = float(args[12]) if args[12] is not None else 0.0
            self.snapshot_ts = self.snap_ts
        else:
            # Keyword constructor from sim_scanner & bridge
            self.ticker = str(kwargs.get("ticker", args[0] if len(args) > 0 else ""))
            self.cand_id = str(kwargs.get("cand_id", kwargs.get("cid", self.ticker)))
            self.cid = self.cand_id
            self.player = str(kwargs.get("player", args[1] if len(args) > 1 else ""))
            self.team = str(kwargs.get("team", args[2] if len(args) > 2 else ""))
            self.opp = str(kwargs.get("opp", args[3] if len(args) > 3 else ""))
            self.stat = str(kwargs.get("stat", args[4] if len(args) > 4 else "PTS"))
            self.line = float(kwargs.get("line", args[5] if len(args) > 5 else 0.0))
            self.side = str(kwargs.get("side", args[6] if len(args) > 6 else "YES")).upper()
            self.sim_prob = float(kwargs.get("sim_prob", kwargs.get("p_yes", args[7] if len(args) > 7 else 0.5)))
            self.p_yes = self.sim_prob
            self.yes_bid = kwargs.get("yes_bid", kwargs.get("bid", args[8] if len(args) > 8 else None))
            self.bid = self.yes_bid
            self.yes_ask = kwargs.get("yes_ask", kwargs.get("ask", args[9] if len(args) > 9 else None))
            self.ask = self.yes_ask
            self.tipoff_ts = float(kwargs.get("tipoff_ts", args[10] if len(args) > 10 else 0.0) or 0.0)
            self.snapshot_ts = float(kwargs.get("snapshot_ts", kwargs.get("snap_ts", args[11] if len(args) > 11 else time.time()) or time.time()))
            self.snap_ts = self.snapshot_ts
            self.confirmed = bool(kwargs.get("confirmed", args[12] if len(args) > 12 else False))
            self.game_id = kwargs.get("game_id", f"{self.team}:{self.opp}")


@dataclass
class Decision:
    candidate: Candidate
    approved: bool
    reason_code: str
    reason_desc: str
    p_mkt: float
    fee: float
    edge: float
    p_shrunk: float
    stake: float                       # Sized stake in dollars
    quarter_kelly: float               # Raw quarter Kelly fraction
    is_live: bool                      # True only if live locks pass
    is_preliminary: bool = False       # True if unconfirmed but > 1h out
    action: str = "VETO"               # "VETO", "APPROVE_PAPER", or "APPROVE_LIVE"
    flags: List[str] = field(default_factory=list)
    reasons: List[Tuple[str, str]] = field(default_factory=list)
    entry: float = 0.0                 # Entry price for display
    cand_id: Optional[str] = None

    def __post_init__(self):
        if self.cand_id is None and self.candidate is not None:
            self.cand_id = getattr(self.candidate, "cand_id", getattr(self.candidate, "cid", None))


def init_risk_db(conn: sqlite3.Connection) -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS risk_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        ticker TEXT,
        player TEXT,
        team TEXT,
        opp TEXT,
        stat TEXT,
        line REAL,
        side TEXT,
        sim_prob REAL,
        mkt_price REAL,
        edge REAL,
        approved INTEGER NOT NULL,
        reason_code TEXT NOT NULL,
        reason_desc TEXT,
        stake REAL,
        is_live INTEGER NOT NULL,
        is_preliminary INTEGER NOT NULL,
        tipoff_ts REAL,
        snapshot_ts REAL
    );
    """
    conn.execute(schema)
    conn.commit()


def log_decision_to_db(decision: Decision, db_path: str = DB_PATH) -> None:
    try:
        con = sqlite3.connect(db_path)
        init_risk_db(con)
        c = decision.candidate
        con.execute(
            """INSERT INTO risk_log (
                ts, ticker, player, team, opp, stat, line, side,
                sim_prob, mkt_price, edge, approved, reason_code, reason_desc,
                stake, is_live, is_preliminary, tipoff_ts, snapshot_ts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(),
                c.ticker,
                c.player,
                c.team,
                c.opp,
                c.stat,
                float(c.line),
                c.side.upper(),
                float(c.sim_prob),
                float(decision.p_mkt),
                float(decision.edge),
                1 if decision.approved else 0,
                decision.reason_code,
                decision.reason_desc,
                float(decision.stake),
                1 if decision.is_live else 0,
                1 if decision.is_preliminary else 0,
                float(c.tipoff_ts) if c.tipoff_ts else None,
                float(c.snapshot_ts) if c.snapshot_ts else None,
            ),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def _norm_price(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    f = float(val)
    return f / 100.0 if f > 1.5 else f


def check_live_lock(config: RiskConfig, db_path: str = DB_PATH) -> Tuple[bool, str]:
    if not config.live_enabled:
        return False, "Live switch is OFF (Paper-only mode)"

    if not os.path.exists(db_path):
        return False, "No settled database found"

    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM sim_log WHERE hit IS NOT NULL AND (yes_bid IS NOT NULL OR yes_ask IS NOT NULL)").fetchall()
        con.close()

        if len(rows) < config.min_settled_props_for_live:
            return False, f"Need >= {config.min_settled_props_for_live} settled props (have {len(rows)})"

        bs_sim = sum((r["sim_prob"] - r["hit"]) ** 2 for r in rows) / len(rows)
        bs_mkt = sum((((r["yes_bid"] + r["yes_ask"]) / 2.0 if (r["yes_bid"] is not None and r["yes_ask"] is not None) else (r["yes_ask"] or r["yes_bid"])) - r["hit"]) ** 2 for r in rows) / len(rows)

        if bs_sim >= bs_mkt:
            return False, f"Sim Brier ({bs_sim:.4f}) does not beat market ({bs_mkt:.4f})"

        return True, "Live lock passed"
    except Exception as exc:
        return False, f"Live lock check error: {exc}"


def _normalize_open_positions(positions: Any) -> Set[str]:
    """Normalizes open positions list/set of strings or dicts into a set of normalized keys."""
    norm: Set[str] = set()
    if not positions:
        return norm
    for item in positions:
        if isinstance(item, str):
            norm.add(item)
        elif isinstance(item, dict):
            ticker = item.get("ticker")
            side = str(item.get("side", "")).upper()
            if ticker and side:
                norm.add(f"{ticker}:{side}")
            if ticker:
                norm.add(ticker)
    return norm


def clear_risk_log(db_path: str = DB_PATH) -> int:
    """Purges corrupted or legacy rows from risk_log table in sim_log.db."""
    try:
        con = sqlite3.connect(db_path)
        init_risk_db(con)
        cur = con.cursor()
        cur.execute("DELETE FROM risk_log")
        deleted = cur.rowcount
        con.commit()
        con.close()
        return deleted
    except Exception:
        return 0


def evaluate_candidate(
    c: Candidate,
    bankroll: float,
    current_day_staked: float = 0.0,
    current_player_staked: float = 0.0,
    current_game_staked: float = 0.0,
    held_positions: Optional[Set[str]] = None,
    daily_pnl: float = 0.0,
    config: Optional[RiskConfig] = None,
    now_ts: Optional[float] = None,
) -> Decision:
    cfg = config or RiskConfig()
    now = now_ts or time.time()
    held = _normalize_open_positions(held_positions)

    stat_norm = c.stat.upper()
    side_u = c.side.upper()

    def _veto(code: str, desc: str, p_mkt: float = 0.5, fee: float = 0.0, edge: float = 0.0, p_shrunk: float = 0.5) -> Decision:
        return Decision(
            candidate=c, approved=False, reason_code=code, reason_desc=desc,
            p_mkt=p_mkt, fee=fee, edge=edge, p_shrunk=p_shrunk, stake=0.0,
            quarter_kelly=0.0, is_live=False, action="VETO", flags=[],
            reasons=[(code, desc)], entry=p_mkt, cand_id=c.cand_id,
        )

    # 1. Validated stat
    if stat_norm not in cfg.validated_stats:
        desc = f"Stat {stat_norm} is paper-only or unvalidated"
        return _veto(STAT_UNVALIDATED, desc)

    # 2. Game already started
    if c.tipoff_ts and now >= c.tipoff_ts:
        return _veto(GAME_STARTED, "Game already tipped off")

    # 3. Stale price
    if c.snapshot_ts and (now - c.snapshot_ts) > cfg.stale_price_seconds:
        return _veto(STALE_PRICE, f"Price snapshot is stale ({int(now - c.snapshot_ts)}s old)")

    # 4. Usable price
    bid = _norm_price(c.yes_bid)
    ask = _norm_price(c.yes_ask)

    if side_u == "YES":
        if ask is None or not (0.01 <= ask <= 0.99):
            return _veto(NO_USABLE_PRICE, "No valid YES ask price")
        cost_price = ask
        p_mkt = ask
    else:
        if bid is None or not (0.01 <= bid <= 0.99):
            return _veto(NO_USABLE_PRICE, "No valid YES bid price")
        cost_price = 1.0 - bid
        p_mkt = 1.0 - bid

    # 5. Sim prob band [20%, 80%]
    p_sim = c.sim_prob if side_u == "YES" else (1.0 - c.sim_prob)
    if not (cfg.prob_min <= p_sim <= cfg.prob_max):
        desc = f"Sim prob {p_sim:.1%} outside [{cfg.prob_min:.0%}, {cfg.prob_max:.0%}]"
        return _veto(PROB_OUT_OF_BOUNDS, desc, p_mkt=p_mkt, p_shrunk=p_sim)

    # 6. Fee and Edge
    fee = cfg.fee_rate * cost_price * (1.0 - cost_price)
    edge_after_fees = p_sim - cost_price - fee

    # 7. Edge under 5 points
    if edge_after_fees < cfg.min_edge_after_fees:
        desc = f"Edge {edge_after_fees:+.1%} below minimum {cfg.min_edge_after_fees:.0%}"
        return _veto(EDGE_TOO_SMALL, desc, p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_sim)

    # 8. Model gap > 15 points
    market_gap = abs(p_sim - p_mkt)
    if market_gap > cfg.max_market_gap:
        desc = f"Gap {market_gap:.1%} between sim and market exceeds 15%"
        return _veto(MODEL_GAP_TOO_WIDE, desc, p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_sim)

    # 9. Lineup unconfirmed within 1 hour
    time_to_tip = (c.tipoff_ts - now) if c.tipoff_ts else 7200.0
    is_preliminary = False

    if not c.confirmed:
        if time_to_tip < 3600.0:
            return _veto(UNCONFIRMED_NEAR_TIP, "Lineup unconfirmed within 1 hour of tip-off", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_sim)
        else:
            is_preliminary = True

    # 10. Opposing side held
    opp_key = f"{c.ticker}:{'NO' if side_u == 'YES' else 'YES'}"
    if opp_key in held:
        return _veto(OPPOSING_SIDE_HELD, f"Already holding opposing side of {c.ticker}", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_sim)

    # 11. Daily loss limit
    if daily_pnl <= -1.0 * (bankroll * cfg.daily_loss_limit_pct):
        desc = f"Daily loss limit hit: PnL ${daily_pnl:.2f}"
        return _veto(DAILY_LOSS_LIMIT, desc, p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_sim)

    # 12. Sizing: Quarter Kelly on shrunk probability
    p_shrunk = p_sim * (1.0 - cfg.shrink_weight) + p_mkt * cfg.shrink_weight
    b_odds = (1.0 - cost_price) / max(0.01, cost_price)
    raw_kelly = (b_odds * p_shrunk - (1.0 - p_shrunk)) / max(0.01, b_odds)
    quarter_kelly = max(0.0, raw_kelly * cfg.kelly_fraction)

    base_stake = bankroll * quarter_kelly
    if is_preliminary:
        base_stake *= cfg.unconfirmed_size_mult

    # 13. Correlation Caps
    max_bet_allowed = bankroll * cfg.cap_per_bet
    if base_stake > max_bet_allowed:
        base_stake = max_bet_allowed

    rem_player = max(0.0, (bankroll * cfg.cap_per_player) - current_player_staked)
    if rem_player <= 0.01:
        return _veto(CAP_REACHED_PLAYER, f"Player cap 3% reached for {c.player}", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_shrunk)
    stake = min(base_stake, rem_player)

    rem_game = max(0.0, (bankroll * cfg.cap_per_game) - current_game_staked)
    if rem_game <= 0.01:
        return _veto(CAP_REACHED_GAME, f"Game cap 5% reached for {c.team} vs {c.opp}", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_shrunk)
    stake = min(stake, rem_game)

    rem_day = max(0.0, (bankroll * cfg.cap_per_day) - current_day_staked)
    if rem_day <= 0.01:
        return _veto(CAP_REACHED_DAY, "Daily portfolio cap 12% reached", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_shrunk)
    stake = min(stake, rem_day)

    stake = round(stake, 2)
    if stake < 0.01:
        return _veto(CAP_REACHED_BET, "Stake sized below minimum $0.01", p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_shrunk)

    # 14. Live Lock
    is_live_allowed, _ = check_live_lock(cfg)
    is_live = bool(is_live_allowed and not is_preliminary)
    action = "APPROVE_LIVE" if is_live else "APPROVE_PAPER"
    flags = ["PRELIMINARY"] if is_preliminary else []

    desc = f"Approved: Sized at ${stake:.2f} ({'PRELIMINARY' if is_preliminary else 'Quarter Kelly'}, edge {edge_after_fees:+.1%})"
    return Decision(
        candidate=c, approved=True, reason_code=APPROVED, reason_desc=desc,
        p_mkt=p_mkt, fee=fee, edge=edge_after_fees, p_shrunk=p_shrunk,
        stake=stake, quarter_kelly=quarter_kelly, is_live=is_live,
        is_preliminary=is_preliminary, action=action, flags=flags,
        reasons=[], entry=p_mkt, cand_id=c.cand_id,
    )


def evaluate_batch(
    candidates: List[Candidate],
    bankroll: float,
    held_positions: Optional[Any] = None,
    daily_pnl: float = 0.0,
    config: Optional[RiskConfig] = None,
    now_ts: Optional[float] = None,
    log_to_db: bool = True,
) -> List[Decision]:
    cfg = config or RiskConfig()
    now = now_ts or time.time()
    held = _normalize_open_positions(held_positions)

    def _rough_edge(cand: Candidate) -> float:
        ask = _norm_price(cand.yes_ask)
        bid = _norm_price(cand.yes_bid)
        cost = ask if cand.side.upper() == "YES" else (1.0 - bid if bid else None)
        if cost is None:
            return -999.0
        p_sim = cand.sim_prob if cand.side.upper() == "YES" else (1.0 - cand.sim_prob)
        fee = cfg.fee_rate * cost * (1.0 - cost)
        return p_sim - cost - fee

    sorted_candidates = sorted(candidates, key=lambda cand: (-_rough_edge(cand), getattr(cand, "cand_id", "")))
    decisions: List[Decision] = []
    staked_day = 0.0
    staked_player: Dict[str, float] = {}
    staked_game: Dict[str, float] = {}
    approved_count = 0

    for c in sorted_candidates:
        if approved_count >= cfg.max_open_positions:
            d = Decision(
                candidate=c, approved=False, reason_code=MAX_POSITIONS_REACHED,
                reason_desc="Max open positions reached",
                p_mkt=0.5, fee=0.0, edge=0.0, p_shrunk=0.5, stake=0.0,
                quarter_kelly=0.0, is_live=False, action="VETO",
                flags=[], reasons=[(MAX_POSITIONS_REACHED, "Max open positions reached")],
                entry=0.5, cand_id=c.cand_id,
            )
            decisions.append(d)
            if log_to_db:
                log_decision_to_db(d)
            continue

        p_key = c.player
        g_key = c.game_id or f"{c.team}:{c.opp}"
        cur_p_staked = staked_player.get(p_key, 0.0)
        cur_g_staked = staked_game.get(g_key, 0.0)

        d = evaluate_candidate(
            c=c, bankroll=bankroll, current_day_staked=staked_day,
            current_player_staked=cur_p_staked, current_game_staked=cur_g_staked,
            held_positions=held, daily_pnl=daily_pnl, config=cfg, now_ts=now,
        )
        decisions.append(d)

        if d.approved:
            staked_day += d.stake
            staked_player[p_key] = cur_p_staked + d.stake
            staked_game[g_key] = cur_g_staked + d.stake
            approved_count += 1
            held.add(f"{c.ticker}:{c.side.upper()}")
            held.add(c.ticker)

        if log_to_db:
            log_decision_to_db(d)

    # Preserve the original candidate order for the caller (zip compatibility)
    cand_decision_map = {id(d.candidate): d for d in decisions}
    return [cand_decision_map[id(c)] for c in candidates]


def evaluate(candidates: List[Candidate], state: Dict[str, Any], config: Optional[RiskConfig] = None, now: Optional[float] = None) -> List[Decision]:
    """team_card.py compatible entry point: evaluate(candidates, state, config, now)."""
    bankroll = float(state.get("bankroll", 100.0))
    daily_pnl = float(state.get("daily_pnl", 0.0))
    open_positions = state.get("open_positions", [])
    return evaluate_batch(
        candidates=candidates,
        bankroll=bankroll,
        held_positions=open_positions,
        daily_pnl=daily_pnl,
        config=config,
        now_ts=now,
        log_to_db=False,
    )


# ----------------------------------------------------------------------------- Self-Test
def self_test():
    print("=" * 70)
    print("  RISK MANAGER SELF-TEST (DETERMINISTIC SUITE)")
    print("=" * 70)

    cfg = RiskConfig(live_enabled=False)
    now = time.time()
    bankroll = 100.0

    cases = [
        (Candidate("c1", "Luka Doncic", "g1", "PRA", 45.5, "YES", 0.65, 0.50, 0.52, "T-PRA", True, now + 7200, now), STAT_UNVALIDATED),
        (Candidate("c2", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.65, 0.50, 0.52, "T-PAST", True, now - 60, now), GAME_STARTED),
        (Candidate("c3", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.65, 0.50, 0.52, "T-STALE", True, now + 7200, now - 1800), STALE_PRICE),
        (Candidate("c4", "Luka Doncic", "g1", "PTS", 15.5, "YES", 0.88, 0.70, 0.72, "T-HIGH", True, now + 7200, now), PROB_OUT_OF_BOUNDS),
        (Candidate("c5", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.54, 0.50, 0.52, "T-LOWEDGE", True, now + 7200, now), EDGE_TOO_SMALL),
        (Candidate("c6", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.68, 0.46, 0.48, "T-GAP", True, now + 7200, now), MODEL_GAP_TOO_WIDE),
        (Candidate("c7", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.60, 0.48, 0.50, "T-LATEUNCONF", False, now + 2700, now), UNCONFIRMED_NEAR_TIP),
        (Candidate("c8", "Luka Doncic", "g1", "PTS", 29.5, "YES", 0.60, 0.48, 0.50, "T-PRELIM", False, now + 7200, now), APPROVED),
        (Candidate("c9", "Anthony Edwards", "g2", "PTS", 24.5, "YES", 0.62, 0.50, 0.52, "T-CLEAN", True, now + 7200, now), APPROVED),
    ]

    all_passed = True
    for idx, (cand, expected_code) in enumerate(cases, 1):
        dec = evaluate_candidate(cand, bankroll=bankroll, config=cfg, now_ts=now)
        match = (dec.reason_code == expected_code)
        if not match:
            all_passed = False
        mark = "✅ PASS" if match else "❌ FAIL"
        print(f"  [{idx}/9] {mark} | {cand.ticker:14s} | Expected: {expected_code:22s} | Got: {dec.reason_code:22s} | Stake: ${dec.stake:.2f}")

    if all_passed:
        print("\n🎉 ALL SELF-TESTS PASSED DETERMINISTICALLY.")
    else:
        print("\n❌ SOME TESTS FAILED.")


if __name__ == "__main__":
    self_test()
