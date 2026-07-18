import json
import time
import os
import sqlite3
from betting_db import get_db, save_parlay

def american_to_decimal(american_odds):
    if american_odds is None:
        return 1.909  # Default decimal odds corresponding to -110
    try:
        odds = float(american_odds)
        if odds > 0:
            return 1.0 + (odds / 100.0)
        else:
            return 1.0 + (100.0 / abs(odds))
    except:
        return 1.909

def decimal_to_american(decimal_odds):
    try:
        dec = float(decimal_odds)
        if dec >= 2.0:
            return int((dec - 1.0) * 100.0)
        else:
            return int(-100.0 / (dec - 1.0))
    except:
        return -110

def build_consensus_parlay():
    """
    Module D: Consensus Matrix, Conflict Resolution, and Parlay Builder Engine.
    1. Fetches all 'Pending' parsed picks logged within the last 24 hours.
    2. Groups picks by unique leg (matchup + target_asset + bet_type + line_value).
    3. Resolves conflicts (e.g. opposing moneyline picks on same matchup).
    4. Combines 2 to 4 top consensus legs into a optimized parlay.
    5. Calculates cumulative payout odds and implied probability.
    6. Saves the generated parlay to the database.
    """
    conn = get_db()
    cursor = conn.cursor()
    
    # Fetch active pending picks in the last 24 hours
    one_day_ago = int(time.time()) - 86400
    cursor.execute("""
        SELECT p.id as pick_id, p.league, p.matchup, p.bet_type, p.target_asset, p.line_value, p.odds, p.trends,
               c.weight, c.handle as capper
        FROM parsed_picks p
        JOIN raw_posts r ON p.post_id = r.id
        JOIN cappers c ON r.capper_id = c.id
        WHERE r.timestamp >= ? AND p.outcome = 'Pending'
    """, (one_day_ago,))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        print("⚠️ No active parsed picks found to compile parlay.")
        return None
        
    # 1. Group by unique leg
    legs = {}
    for row in rows:
        pick = dict(row)
        # Create unique key for the leg (ignore slight roster casing differences)
        matchup_norm = pick["matchup"].lower().strip()
        asset_norm = pick["target_asset"].lower().strip()
        bet_norm = pick["bet_type"].lower().strip()
        line_norm = str(pick["line_value"]) if pick["line_value"] is not None else ""
        
        leg_key = f"{matchup_norm}|{asset_norm}|{bet_norm}|{line_norm}"
        
        if leg_key not in legs:
            # Safely parse trends from JSON
            trends_list = []
            if pick.get("trends"):
                try:
                    trends_list = json.loads(pick["trends"])
                except:
                    pass
            legs[leg_key] = {
                "league": pick["league"],
                "matchup": pick["matchup"],
                "bet_type": pick["bet_type"],
                "target_asset": pick["target_asset"],
                "line_value": pick["line_value"],
                "odds": pick["odds"] or -110,
                "trends": trends_list,
                "cappers": [pick["capper"]],
                "score": float(pick["weight"]), # Weighted consensus score
                "raw_keys": [pick["pick_id"]]
            }
        else:
            legs[leg_key]["cappers"].append(pick["capper"])
            legs[leg_key]["score"] += float(pick["weight"])
            legs[leg_key]["raw_keys"].append(pick["pick_id"])
            
    # Convert back to list and sort by consensus score (descending)
    leg_list = list(legs.values())
    leg_list.sort(key=lambda x: x["score"], reverse=True)
    
    # 2. Conflict Resolution
    # Identify conflicting legs on the same matchup
    # Conflict conditions:
    # - Same matchup but different target assets (e.g. Dallas ML vs Boston ML)
    # - Opposing spread lines (e.g. Dallas -3 vs Boston +3)
    final_legs = []
    seen_matchups = set()
    
    for leg in leg_list:
        matchup_key = leg["matchup"].lower().strip()
        
        if matchup_key in seen_matchups:
            # Matchup already has a leg selected, check for conflicts
            # To prevent opposing side bets, we skip any opposing leg on this game
            print(f"🚨 Conflict Resolution: Opposing pick for matchup '{leg['matchup']}' detected. Skipping leg for '{leg['target_asset']}'.")
            continue
            
        final_legs.append(leg)
        seen_matchups.add(matchup_key)
        
    # Cap legs between 2 and 4 to build optimized parlay
    if len(final_legs) < 2:
        print("⚠️ Not enough non-conflicting legs to form a parlay (minimum 2 legs required).")
        return None
        
    parlay_legs = final_legs[:4]
    
    # 3. Calculate Cumulative Multipliers
    decimal_odds_product = 1.0
    consensus_level = 0
    
    for leg in parlay_legs:
        # Average consensus level is average score
        consensus_level += len(leg["cappers"])
        dec_o = american_to_decimal(leg["odds"])
        decimal_odds_product *= dec_o
        
    total_odds_american = decimal_to_american(decimal_odds_product)
    implied_prob = 1.0 / decimal_odds_product
    avg_consensus = round(consensus_level / len(parlay_legs), 1)
    
    parlay_data = {
        "legs": parlay_legs,
        "total_odds_american": total_odds_american,
        "implied_probability_percent": round(implied_prob * 100, 1),
        "consensus_level": avg_consensus,
        "timestamp": int(time.time())
    }
    
    # 4. Save to database
    save_parlay(parlay_data, total_odds_american, avg_consensus)
    print(f"📊 Parlay built successfully! Total Legs: {len(parlay_legs)} | Odds: {total_odds_american} | Implied Prob: {parlay_data['implied_probability_percent']}%")
    return parlay_data
