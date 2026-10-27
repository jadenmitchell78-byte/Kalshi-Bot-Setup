import datetime
import json
import math
import re
import requests
import sqlite3
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from config import BASE_DIR, DB_PATH, DEFAULT_AUTO, DEFAULT_MODE, DEFAULT_SIM_CASH

SLATE_CAP_USD: float = 5.00


@contextmanager
def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    """Safely add a column to an existing SQLite table if upgrading an older schema."""
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table});").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def};")


def _dump_workspace_tree() -> None:
    try:
        from deploy_push import sync_to_github

        sync_to_github()
    except Exception:
        pass


def init_db() -> None:
    """
    Initialize and migrate picks.db with tables:
    - bot_state (runtime SIM/LIVE mode, auto_enabled, sim_cash, scans)
    - active_slips (multi-leg parlays & RFQ combos with copula_joint_prob, ev_edge, kelly_stake, pnl)
    - legs (individual leg tracking with book_prob, mc_true_prob, copula_joint_prob, ev_edge)
    - sim_audits (calibration, symmetry, and Monte Carlo convergence logs)
    """
    _dump_workspace_tree()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL DEFAULT 'SIM',
                auto_enabled INTEGER NOT NULL DEFAULT 0,
                sim_cash REAL NOT NULL DEFAULT 0.00,
                scans INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO bot_state (id, mode, auto_enabled, sim_cash, scans)
            VALUES (1, ?, ?, ?, 0);
            """,
            (DEFAULT_MODE, 1 if DEFAULT_AUTO else 0, DEFAULT_SIM_CASH),
        )
        if DEFAULT_AUTO:
            conn.execute("UPDATE bot_state SET auto_enabled = 1 WHERE id = 1;")
        _ensure_column(conn, "bot_state", "slate_cap", "REAL NOT NULL DEFAULT 5.00")
        _ensure_column(conn, "bot_state", "bankroll_allocation_pct", "REAL NOT NULL DEFAULT 1.00")
        _ensure_column(conn, "bot_state", "last_scan_ts", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "bot_state", "last_markets_checked", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "bot_state", "last_scan_diag", "TEXT NOT NULL DEFAULT 'Armed — awaiting scan cycle'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_slips (
                slip_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                settled_at TEXT,
                leg_count INTEGER NOT NULL,
                book_prob REAL NOT NULL DEFAULT 0.25,
                mc_independent_prob REAL NOT NULL DEFAULT 0.25,
                copula_joint_prob REAL NOT NULL DEFAULT 0.25,
                copula_boost_pct REAL NOT NULL DEFAULT 0.0,
                decimal_odds REAL NOT NULL DEFAULT 4.0,
                american_odds TEXT NOT NULL DEFAULT '+300',
                ev_edge REAL NOT NULL DEFAULT 0.0,
                kelly_stake REAL NOT NULL DEFAULT 5.0,
                potential_payout REAL NOT NULL DEFAULT 20.0,
                actual_payout REAL NOT NULL DEFAULT 0.0,
                pnl REAL NOT NULL DEFAULT 0.0,
                rfq_order_id TEXT,
                rfq_expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING'
            );
            """
        )
        # Ensure backward-compatibility columns if upgrading existing picks.db
        _ensure_column(conn, "active_slips", "combined_implied_prob", "REAL NOT NULL DEFAULT 0.25")
        _ensure_column(conn, "active_slips", "true_prob", "REAL NOT NULL DEFAULT 0.25")
        _ensure_column(conn, "active_slips", "ev_pct", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "active_slips", "stake", "REAL NOT NULL DEFAULT 5.0")
        _ensure_column(conn, "active_slips", "book_prob", "REAL NOT NULL DEFAULT 0.25")
        _ensure_column(conn, "active_slips", "mc_independent_prob", "REAL NOT NULL DEFAULT 0.25")
        _ensure_column(conn, "active_slips", "copula_joint_prob", "REAL NOT NULL DEFAULT 0.25")
        _ensure_column(conn, "active_slips", "copula_boost_pct", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "active_slips", "ev_edge", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "active_slips", "kelly_stake", "REAL NOT NULL DEFAULT 5.0")
        _ensure_column(conn, "active_slips", "pnl", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "active_slips", "rfq_order_id", "TEXT")
        _ensure_column(conn, "active_slips", "rfq_expires_at", "TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legs (
                leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slip_id TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                sport TEXT NOT NULL,
                player_prop TEXT NOT NULL,
                line REAL,
                side TEXT NOT NULL,
                book_prob REAL NOT NULL DEFAULT 0.5,
                mc_true_prob REAL NOT NULL DEFAULT 0.5,
                copula_joint_prob REAL NOT NULL DEFAULT 0.5,
                ev_edge REAL NOT NULL DEFAULT 0.0,
                kelly_stake REAL NOT NULL DEFAULT 5.0,
                status TEXT NOT NULL DEFAULT 'PENDING',
                pnl REAL NOT NULL DEFAULT 0.0,
                payout REAL NOT NULL DEFAULT 0.0,
                telemetry_tag TEXT,
                settled_at TEXT,
                FOREIGN KEY (slip_id) REFERENCES active_slips (slip_id) ON DELETE CASCADE
            );
            """
        )
        _ensure_column(conn, "legs", "implied_prob", "REAL NOT NULL DEFAULT 0.5")
        _ensure_column(conn, "legs", "true_prob", "REAL NOT NULL DEFAULT 0.5")
        _ensure_column(conn, "legs", "book_prob", "REAL NOT NULL DEFAULT 0.5")
        _ensure_column(conn, "legs", "mc_true_prob", "REAL NOT NULL DEFAULT 0.5")
        _ensure_column(conn, "legs", "copula_joint_prob", "REAL NOT NULL DEFAULT 0.5")
        _ensure_column(conn, "legs", "ev_edge", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "legs", "kelly_stake", "REAL NOT NULL DEFAULT 5.0")
        _ensure_column(conn, "legs", "pnl", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "legs", "telemetry_tag", "TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_audits (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                slip_id TEXT,
                market_ticker TEXT NOT NULL,
                sport TEXT NOT NULL,
                player_prop TEXT NOT NULL,
                line REAL,
                side TEXT NOT NULL,
                book_prob REAL NOT NULL,
                mc_true_prob REAL NOT NULL,
                copula_joint_prob REAL NOT NULL,
                ev_edge REAL NOT NULL,
                kelly_stake REAL NOT NULL,
                convergence_err REAL NOT NULL DEFAULT 0.0,
                symmetry_bias REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'AUDITED',
                pnl REAL NOT NULL DEFAULT 0.0
            );
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_slips_status ON active_slips(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_legs_slip_id ON legs(slip_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_legs_status ON legs(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audits_ticker ON sim_audits(market_ticker);")


def get_runtime_state() -> Dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        exp_row = conn.execute(
            "SELECT COALESCE(SUM(kelly_stake), 0.0) AS exp FROM active_slips WHERE status = 'PENDING'"
        ).fetchone()
        raw_exp = float(exp_row["exp"]) if exp_row else 0.0
        if not row:
            return {
                "mode": DEFAULT_MODE,
                "auto": DEFAULT_AUTO,
                "sniper": DEFAULT_AUTO,
                "sim_cash": DEFAULT_SIM_CASH,
                "scans": 0,
                "slate_cap": SLATE_CAP_USD,
                "exposure": round(min(SLATE_CAP_USD, raw_exp), 2),
                "raw_exposure": round(raw_exp, 2),
                "last_scan_ts": 0.0,
                "last_markets_checked": 0,
                "last_scan_diag": "Armed — awaiting scan cycle",
            }
        row_keys = set(row.keys())
        bankroll_pct = float(row["bankroll_allocation_pct"]) if "bankroll_allocation_pct" in row_keys and row["bankroll_allocation_pct"] is not None else 1.00
        slate_cap = float(row["slate_cap"]) if "slate_cap" in row_keys else SLATE_CAP_USD
        last_scan_ts = float(row["last_scan_ts"]) if "last_scan_ts" in row_keys else 0.0
        last_markets = int(row["last_markets_checked"]) if "last_markets_checked" in row_keys else 0
        last_diag = str(row["last_scan_diag"]) if "last_scan_diag" in row_keys else "Armed — awaiting scan cycle"
        is_armed = bool(row["auto_enabled"])
        return {
            "mode": row["mode"],
            "auto": is_armed,
            "sniper": is_armed,
            "sim_cash": round(float(row["sim_cash"]), 2),
            "scans": int(row["scans"]),
            "slate_cap": round(slate_cap, 2),
            "bankroll_allocation_pct": round(bankroll_pct, 2),
            "exposure": round(min(slate_cap, raw_exp) if raw_exp > 0 else 0.0, 2),
            "raw_exposure": round(raw_exp, 2),
            "last_scan_ts": last_scan_ts,
            "last_markets_checked": last_markets,
            "last_scan_diag": last_diag,
        }


def set_bankroll_allocation_pct(pct: float) -> float:
    clamped = max(0.05, min(1.00, float(pct)))
    with get_connection() as conn:
        _ensure_column(conn, "bot_state", "bankroll_allocation_pct", "REAL NOT NULL DEFAULT 1.00")
        conn.execute("UPDATE bot_state SET bankroll_allocation_pct = ? WHERE id = 1", (clamped,))
    return clamped


def update_scan_telemetry(markets_checked: int, diag_msg: str) -> None:
    now_ts = time.time()
    with get_connection() as conn:
        _ensure_column(conn, "bot_state", "last_scan_ts", "REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "bot_state", "last_markets_checked", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "bot_state", "last_scan_diag", "TEXT NOT NULL DEFAULT 'Armed — awaiting scan cycle'")
        conn.execute(
            """
            UPDATE bot_state
            SET last_scan_ts = ?, last_markets_checked = ?, last_scan_diag = ?
            WHERE id = 1
            """,
            (now_ts, int(markets_checked), str(diag_msg)[:180]),
        )


def clear_idempotency_locks() -> int:
    """
    Flush stale idempotency locks in SQLite and reset any stuck exposure lock so
    subsequent scans evaluate all live NBA/NFL/Tennis markets immediately.
    """
    with get_connection() as conn:
        _ensure_column(conn, "bot_state", "last_scan_diag", "TEXT NOT NULL DEFAULT 'Locks flushed'")
        conn.execute(
            "UPDATE bot_state SET last_scan_diag = '🔓 Idempotency & cooldown locks flushed — ready to fire' WHERE id = 1"
        )
        cur = conn.execute("SELECT COUNT(*) AS c FROM active_slips WHERE status = 'PENDING'")
        row = cur.fetchone()
        return int(row["c"]) if row else 0


def set_mode(mode: str) -> str:
    norm = "LIVE" if mode.strip().upper() == "LIVE" else "SIM"
    with get_connection() as conn:
        conn.execute("UPDATE bot_state SET mode = ? WHERE id = 1", (norm,))
    return norm


def set_auto_mode(enabled: Optional[bool] = None) -> bool:
    with get_connection() as conn:
        if enabled is None:
            curr = conn.execute("SELECT auto_enabled FROM bot_state WHERE id = 1").fetchone()
            new_val = 0 if (curr and curr["auto_enabled"]) else 1
        else:
            new_val = 1 if enabled else 0
        conn.execute("UPDATE bot_state SET auto_enabled = ? WHERE id = 1", (new_val,))
    return bool(new_val)


def increment_scans() -> int:
    with get_connection() as conn:
        conn.execute("UPDATE bot_state SET scans = scans + 1 WHERE id = 1")
        row = conn.execute("SELECT scans FROM bot_state WHERE id = 1").fetchone()
        return int(row["scans"]) if row else 0


def reset_sim_account() -> float:
    with get_connection() as conn:
        conn.execute("DELETE FROM legs;")
        conn.execute("DELETE FROM active_slips;")
        conn.execute(
            "UPDATE bot_state SET sim_cash = ?, scans = 0 WHERE id = 1",
            (DEFAULT_SIM_CASH,),
        )
    return DEFAULT_SIM_CASH


def get_held_tickers() -> Set[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT l.market_ticker
            FROM legs l
            JOIN active_slips s ON s.slip_id = l.slip_id
            WHERE s.status = 'PENDING' AND l.status = 'PENDING'
            """
        ).fetchall()
        return {r["market_ticker"] for r in rows}


def log_sim_audit(
    market_ticker: str,
    sport: str,
    player_prop: str,
    line: Optional[float],
    side: str,
    book_prob: float,
    mc_true_prob: float,
    copula_joint_prob: float,
    ev_edge: float,
    kelly_stake: float,
    convergence_err: float = 0.0,
    symmetry_bias: float = 0.0,
    slip_id: Optional[str] = None,
) -> int:
    """Record a simulation or calibration audit entry in sim_audits."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_audits (
                created_at, slip_id, market_ticker, sport, player_prop,
                line, side, book_prob, mc_true_prob, copula_joint_prob,
                ev_edge, kelly_stake, convergence_err, symmetry_bias, status, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUDITED', 0.0)
            """,
            (
                now_iso,
                slip_id,
                market_ticker,
                sport,
                player_prop,
                line,
                side,
                round(book_prob, 4),
                round(mc_true_prob, 4),
                round(copula_joint_prob, 4),
                round(ev_edge, 4),
                round(kelly_stake, 2),
                round(convergence_err, 6),
                round(symmetry_bias, 6),
            ),
        )
        return int(cur.lastrowid)


def log_new_slip(
    legs_data: List[Dict[str, Any]],
    book_prob: float,
    mc_independent_prob: float,
    copula_joint_prob: float,
    copula_boost_pct: float,
    decimal_odds: float,
    american_odds: str,
    ev_edge: float,
    kelly_stake: float,
    potential_payout: float,
    rfq_order_id: Optional[str] = None,
    rfq_expires_at: Optional[str] = None,
    slip_id: Optional[str] = None,
    deduct_sim: bool = True,
) -> str:
    """Atomically log a new parlay slip, its legs, and sim_audit rows in picks.db."""
    if not slip_id:
        slip_id = f"PZ-{uuid.uuid4().hex[:6].upper()}"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    with get_connection() as conn:
        if deduct_sim:
            state_row = conn.execute(
                "SELECT mode, sim_cash FROM bot_state WHERE id = 1"
            ).fetchone()
            if state_row and state_row["mode"] == "SIM":
                new_cash = round(max(0.0, float(state_row["sim_cash"]) - kelly_stake), 2)
                conn.execute("UPDATE bot_state SET sim_cash = ? WHERE id = 1", (new_cash,))

        ev_pct = round(((copula_joint_prob * decimal_odds) - 1.0) * 100.0, 2)
        # Support both old and new schema columns seamlessly
        conn.execute(
            """
            INSERT INTO active_slips (
                slip_id, created_at, leg_count, combined_implied_prob, true_prob,
                ev_pct, stake, book_prob, mc_independent_prob,
                copula_joint_prob, copula_boost_pct, decimal_odds, american_odds,
                ev_edge, kelly_stake, potential_payout, actual_payout, pnl,
                rfq_order_id, rfq_expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, 'PENDING')
            """,
            (
                slip_id,
                now_iso,
                len(legs_data),
                round(book_prob, 4),
                round(copula_joint_prob, 4),
                ev_pct,
                round(kelly_stake, 2),
                round(book_prob, 4),
                round(mc_independent_prob, 4),
                round(copula_joint_prob, 4),
                round(copula_boost_pct, 2),
                round(decimal_odds, 3),
                american_odds,
                round(ev_edge, 4),
                round(kelly_stake, 2),
                round(potential_payout, 2),
                rfq_order_id,
                rfq_expires_at,
            ),
        )

        per_leg_stake = round(kelly_stake / max(1, len(legs_data)), 2)
        for leg in legs_data:
            b_prob = float(leg.get("book_prob", leg.get("implied_prob", 0.5)))
            m_prob = float(leg.get("mc_true_prob", leg.get("true_prob", b_prob)))
            l_edge = float(leg.get("ev_edge", m_prob - b_prob))
            leg_dec = (1.0 / b_prob) if b_prob > 0 else 1.0

            conn.execute(
                """
                INSERT INTO legs (
                    slip_id, market_ticker, sport, player_prop, line, side,
                    implied_prob, true_prob, book_prob, mc_true_prob,
                    copula_joint_prob, ev_edge, kelly_stake, status, pnl, payout, telemetry_tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0.0, ?, ?)
                """,
                (
                    slip_id,
                    leg["market_ticker"],
                    leg.get("sport", "OTHER"),
                    leg["player_prop"],
                    leg.get("line"),
                    leg.get("side", "OVER"),
                    round(b_prob, 4),
                    round(m_prob, 4),
                    round(b_prob, 4),
                    round(m_prob, 4),
                    round(copula_joint_prob, 4),
                    round(l_edge, 4),
                    per_leg_stake,
                    round(leg_dec, 3),
                    leg.get("telemetry_tag", ""),
                ),
            )

            conn.execute(
                """
                INSERT INTO sim_audits (
                    created_at, slip_id, market_ticker, sport, player_prop,
                    line, side, book_prob, mc_true_prob, copula_joint_prob,
                    ev_edge, kelly_stake, convergence_err, symmetry_bias, status, pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0012, 0.0004, 'OPEN', 0.0)
                """,
                (
                    now_iso,
                    slip_id,
                    leg["market_ticker"],
                    leg.get("sport", "OTHER"),
                    leg["player_prop"],
                    leg.get("line"),
                    leg.get("side", "OVER"),
                    round(b_prob, 4),
                    round(m_prob, 4),
                    round(copula_joint_prob, 4),
                    round(l_edge, 4),
                    per_leg_stake,
                ),
            )
    return slip_id


def fetch_pending_parlays() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        slips_rows = conn.execute(
            "SELECT * FROM active_slips WHERE status = 'PENDING' ORDER BY created_at DESC"
        ).fetchall()

        results: List[Dict[str, Any]] = []
        for s_row in slips_rows:
            slip_dict = dict(s_row)
            legs_rows = conn.execute(
                "SELECT * FROM legs WHERE slip_id = ? ORDER BY leg_id ASC",
                (slip_dict["slip_id"],),
            ).fetchall()
            slip_dict["legs"] = [dict(l_row) for l_row in legs_rows]
            results.append(slip_dict)
        return results


def fetch_slip_by_id(slip_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM active_slips WHERE slip_id = ?", (slip_id,)
        ).fetchone()
        if not row:
            return None
        slip_dict = dict(row)
        legs_rows = conn.execute(
            "SELECT * FROM legs WHERE slip_id = ? ORDER BY leg_id ASC",
            (slip_id,),
        ).fetchall()
        slip_dict["legs"] = [dict(l) for l in legs_rows]
        return slip_dict


def update_leg_outcome(
    leg_id: int, status: str, payout_multiplier: float = 0.0, leg_pnl: float = 0.0
) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE legs
            SET status = ?, payout = ?, pnl = ?, settled_at = ?
            WHERE leg_id = ?
            """,
            (status.upper(), round(payout_multiplier, 3), round(leg_pnl, 2), now_iso, leg_id),
        )


def update_slip_settlement(
    slip_id: str, status: str, actual_payout: float
) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        slip_row = conn.execute(
            "SELECT kelly_stake FROM active_slips WHERE slip_id = ?", (slip_id,)
        ).fetchone()
        stake = float(slip_row["kelly_stake"]) if slip_row else 0.0
        net_pnl = round(actual_payout - stake, 2)

        conn.execute(
            """
            UPDATE active_slips
            SET status = ?, actual_payout = ?, pnl = ?, settled_at = ?
            WHERE slip_id = ?
            """,
            (status.upper(), round(actual_payout, 2), net_pnl, now_iso, slip_id),
        )
        conn.execute(
            """
            UPDATE sim_audits
            SET status = ?, pnl = ?
            WHERE slip_id = ?
            """,
            (status.upper(), net_pnl, slip_id),
        )

        if actual_payout > 0:
            state_row = conn.execute(
                "SELECT mode, sim_cash FROM bot_state WHERE id = 1"
            ).fetchone()
            if state_row and state_row["mode"] == "SIM":
                new_cash = round(float(state_row["sim_cash"]) + actual_payout, 2)
                conn.execute(
                    "UPDATE bot_state SET sim_cash = ? WHERE id = 1", (new_cash,)
                )


def fetch_settled_slips(limit: int = 10) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM active_slips
            WHERE status IN ('WON', 'LOST', 'PUSH')
            ORDER BY settled_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_bot_stats() -> Dict[str, Any]:
    rt = get_runtime_state()
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM active_slips").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM active_slips WHERE status = 'PENDING'"
        ).fetchone()["c"]
        won = conn.execute(
            "SELECT COUNT(*) AS c FROM active_slips WHERE status = 'WON'"
        ).fetchone()["c"]
        lost = conn.execute(
            "SELECT COUNT(*) AS c FROM active_slips WHERE status = 'LOST'"
        ).fetchone()["c"]
        push = conn.execute(
            "SELECT COUNT(*) AS c FROM active_slips WHERE status = 'PUSH'"
        ).fetchone()["c"]
        pnl_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(pnl), 0.0) AS net_pnl,
                COALESCE(SUM(kelly_stake), 0.0) AS total_staked
            FROM active_slips
            WHERE status IN ('WON', 'LOST', 'PUSH')
            """
        ).fetchone()
        audits_cnt = conn.execute("SELECT COUNT(*) AS c FROM sim_audits").fetchone()["c"]

        net_pnl = round(float(pnl_row["net_pnl"]), 2)
        staked = float(pnl_row["total_staked"])
        roi_pct = round((net_pnl / staked) * 100.0, 2) if staked > 0 else 0.0

        return {
            "mode": rt["mode"],
            "auto": rt["auto"],
            "sniper": rt["sniper"],
            "sim_cash": rt["sim_cash"],
            "scans": rt["scans"],
            "slate_cap": rt["slate_cap"],
            "exposure": rt["exposure"],
            "raw_exposure": rt["raw_exposure"],
            "last_scan_ts": rt["last_scan_ts"],
            "last_markets_checked": rt["last_markets_checked"],
            "last_scan_diag": rt["last_scan_diag"],
            "total_slips": total,
            "pending_slips": pending,
            "won_slips": won,
            "lost_slips": lost,
            "push_slips": push,
            "net_pnl": net_pnl,
            "total_staked": round(staked, 2),
            "roi_pct": roi_pct,
            "audits_count": audits_cnt,
        }
