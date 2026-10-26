# ~/peezy_bot/hoops_filter.py
import re

def is_valid_hoops_market(market: dict) -> bool:
    """
    Strictly filters out novelty, retirement, ownership, award, and pop-culture futures
    even if Kalshi lists them under an NBA/NFL series code.
    """
    ticker = str(market.get("ticker", "")).upper()
    title = str(market.get("title", "")).upper()
    subtitle = str(market.get("subtitle", "")).upper()
    combined = f"{ticker} {title} {subtitle}"

    # 1. Must contain an actual basketball/football stat keyword
    VALID_STAT_KEYWORDS = [
        "PTS", "POINT", "REB", "REBOUND", "AST", "ASSIST", 
        "3PT", "THREE", "PRA", "BLK", "BLOCK", "STL", "STEAL",
        "PASS YDS", "RUSH YDS", "REC YDS", "RECEPTIONS", "TOUCHDOWN"
    ]
    has_stat = any(kw in combined for kw in VALID_STAT_KEYWORDS)
    if not has_stat:
        return False

    # 2. STRICTLY REJECT novelty, future, award, and non-game keywords
    FORBIDDEN_KEYWORDS = [
        "RETIRE", "ANNOUNCE", "OWNER", "HEADLINER", "DRAFT", 
        "MVP", "CHAMPIONSHIP", "SELECT", "ELECT", "PRESIDENT", 
        "AWARD", "ALL-STAR", "HALL OF FAME", "TRADED", "SIGN", "BORN"
    ]
    has_forbidden = any(fk in combined for fk in FORBIDDEN_KEYWORDS)
    if has_forbidden:
        return False

    # 3. Must have a valid numeric line/threshold (e.g., 21.5, 3.5, 85.5)
    has_line = bool(re.search(r"\d+(\.\d+)?", combined))
    
    return has_stat and not has_forbidden and has_line
