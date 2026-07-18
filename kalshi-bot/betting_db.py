import sqlite3
import json
import time
import os

DB_FILE = "betting_system.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the SQLite database with required tables."""
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Cappers table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cappers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handle TEXT UNIQUE,
        platform TEXT,
        weight REAL DEFAULT 1.0,
        win_rate REAL DEFAULT 0.5,
        roi REAL DEFAULT 0.0,
        picks_count INTEGER DEFAULT 0
    )
    """)
    
    # 2. Raw posts table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        capper_id INTEGER,
        original_text TEXT,
        timestamp INTEGER,
        status TEXT, -- 'Parsed', 'Failed', 'No Pick'
        FOREIGN KEY(capper_id) REFERENCES cappers(id)
    )
    """)
    
    # 3. Parsed picks table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS parsed_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER,
        league TEXT,
        matchup TEXT,
        bet_type TEXT, -- 'Moneyline', 'Spread', 'Player Prop Over', 'Player Prop Under', etc.
        target_asset TEXT, -- Team name or Player name
        line_value REAL,
        odds REAL,
        outcome TEXT DEFAULT 'Pending', -- 'Pending', 'Win', 'Loss',
        trends TEXT,
        FOREIGN KEY(post_id) REFERENCES raw_posts(id)
    )
    """)
    
    # 4. Parlays table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS parlays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        combined_legs TEXT, -- JSON array of parsed_picks details
        total_odds REAL,
        timestamp INTEGER,
        consensus_level INTEGER
    )
    """)
    
    # Insert default monitored handles if empty
    cursor.execute("SELECT COUNT(*) FROM cappers")
    if cursor.fetchone()[0] == 0:
        default_cappers = [
            ("lockedlines", "x", 1.2, 0.58, 12.4, 45),
            ("dougspicks", "instagram", 1.1, 0.56, 8.5, 38),
            ("deshawnsdubs", "x", 1.0, 0.54, 4.2, 50),
            ("sharprsports", "instagram", 1.0, 0.55, 6.1, 29),
            ("popularparlay", "x", 1.3, 0.61, 18.2, 60)
        ]
        cursor.executemany(
            "INSERT INTO cappers (handle, platform, weight, win_rate, roi, picks_count) VALUES (?, ?, ?, ?, ?, ?)",
            default_cappers
        )
        
    # Migration: add trends column to parsed_picks if it doesn't exist
    try:
        cursor.execute("ALTER TABLE parsed_picks ADD COLUMN trends TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    print("✅ SQLite database initialized.")

# --- Capper helpers ---
def add_capper(handle, platform, weight=1.0):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO cappers (handle, platform, weight) VALUES (?, ?, ?)",
            (handle.lower().strip(), platform.lower().strip(), weight)
        )
        conn.commit()
        return True, "Capper added successfully."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()

def remove_capper(handle):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM cappers WHERE handle = ?", (handle.lower().strip(),))
        conn.commit()
        return True, f"Capper {handle} removed."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()

def get_cappers():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM cappers ORDER BY win_rate DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# --- Post & Pick helpers ---
def log_raw_post(capper_handle, original_text, timestamp):
    conn = get_db()
    cursor = conn.cursor()
    
    # Resolve capper_id
    cursor.execute("SELECT id FROM cappers WHERE handle = ?", (capper_handle.lower(),))
    row = cursor.fetchone()
    if not row:
        # Auto register if not found
        cursor.execute("INSERT INTO cappers (handle, platform) VALUES (?, 'x')", (capper_handle.lower(),))
        capper_id = cursor.lastrowid
    else:
        capper_id = row["id"]
        
    cursor.execute(
        "INSERT INTO raw_posts (capper_id, original_text, timestamp, status) VALUES (?, ?, ?, 'Pending')",
        (capper_id, original_text, timestamp)
    )
    post_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return post_id

def save_parsed_pick(post_id, league, matchup, bet_type, target_asset, line_value, odds, trends=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO parsed_picks (post_id, league, matchup, bet_type, target_asset, line_value, odds, trends)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (post_id, league, matchup, bet_type, target_asset, line_value, odds, json.dumps(trends) if trends else None)
    )
    pick_id = cursor.lastrowid
    cursor.execute("UPDATE raw_posts SET status = 'Parsed' WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return pick_id

def update_post_status(post_id, status):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE raw_posts SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()
    conn.close()

def get_recent_picks(limit=50):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id as pick_id, p.league, p.matchup, p.bet_type, p.target_asset, p.line_value, p.odds, p.outcome,
               r.original_text, r.timestamp, c.handle as capper
        FROM parsed_picks p
        JOIN raw_posts r ON p.post_id = r.id
        JOIN cappers c ON r.capper_id = c.id
        ORDER BY r.timestamp DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def override_parsed_pick(pick_id, league, matchup, bet_type, target_asset, line_value, odds):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE parsed_picks
            SET league = ?, matchup = ?, bet_type = ?, target_asset = ?, line_value = ?, odds = ?
            WHERE id = ?
            """,
            (league, matchup, bet_type, target_asset, line_value, odds, pick_id)
        )
        conn.commit()
        return True, "Pick overridden successfully."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()

def update_pick_outcome(pick_id, outcome):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE parsed_picks SET outcome = ? WHERE id = ?", (outcome, pick_id))
    
    # Recalculate capper stats if pick outcome changed
    cursor.execute("""
        SELECT r.capper_id 
        FROM parsed_picks p
        JOIN raw_posts r ON p.post_id = r.id
        WHERE p.id = ?
    """, (pick_id,))
    capper_row = cursor.fetchone()
    if capper_row:
        capper_id = capper_row["capper_id"]
        # Fetch all resolved picks for this capper
        cursor.execute("""
            SELECT outcome FROM parsed_picks p
            JOIN raw_posts r ON p.post_id = r.id
            WHERE r.capper_id = ? AND p.outcome IN ('Win', 'Loss')
        """, (capper_id,))
        outcomes = [r["outcome"] for r in cursor.fetchall()]
        
        if outcomes:
            wins = sum(1 for o in outcomes if o == 'Win')
            win_rate = wins / len(outcomes)
            # Simulate ROI (Win yields +90% profit, Loss yields -100%)
            total_return = sum(0.9 if o == 'Win' else -1.0 for o in outcomes)
            roi = (total_return / len(outcomes)) * 100
            picks_count = len(outcomes)
            
            cursor.execute(
                "UPDATE cappers SET win_rate = ?, roi = ?, picks_count = ? WHERE id = ?",
                (round(win_rate, 3), round(roi, 1), picks_count, capper_id)
            )
            
    conn.commit()
    conn.close()

# --- Parlay Archive ---
def save_parlay(combined_legs, total_odds, consensus_level):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO parlays (combined_legs, total_odds, timestamp, consensus_level) VALUES (?, ?, ?, ?)",
        (json.dumps(combined_legs), total_odds, int(time.time()), consensus_level)
    )
    conn.commit()
    conn.close()

def get_latest_parlay():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM parlays ORDER BY timestamp DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        res = dict(row)
        res["combined_legs"] = json.loads(res["combined_legs"])
        return res
    return None

def get_underdog_link_for_pick(pick_id):
    """Query DB for original text of pick and extract any shared Underdog link."""
    import re
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT r.original_text 
            FROM parsed_picks p 
            JOIN raw_posts r ON p.post_id = r.id 
            WHERE p.id = ?
        """, (pick_id,))
        row = cursor.fetchone()
        if row and row["original_text"]:
            ud_match = re.search(r"(https?://)?(?:play\.)?underdogfantasy\.com/(?:es-|p-)[a-zA-Z0-9_-]+", row["original_text"])
            if ud_match:
                link = ud_match.group(0)
                if not link.startswith("http"):
                    link = "https://" + link
                return link
    except Exception as e:
        print(f"Error extracting underdog link for pick {pick_id}: {e}")
    finally:
        conn.close()
    return None

def format_promo_link(link, capper_name=None):
    """Dynamically format the referral link label/action based on domain (Kalshi vs Underdog)."""
    if "kalshi.com" in link:
        label = "Kalshi"
        action = "View Profile / Bets Here"
    else:
        label = "Underdog Fantasy"
        action = "Play / Claim Deposit Match Here"
        
    capper_str = f" {capper_name}" if capper_name else ""
    return (
        f"🎯 *Trail{capper_str} on {label}*:\n"
        f"👉 [{action}]({link})"
    )

# Initialize on import
if not os.path.exists(DB_FILE):
    init_db()
