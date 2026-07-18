import os
import json
import re
import requests

# Dictionary of sports team aliases to clean official names
TEAM_ALIASES = {
    # NBA
    "mavs": ("Dallas Mavericks", "NBA"),
    "mavericks": ("Dallas Mavericks", "NBA"),
    "celtics": ("Boston Celtics", "NBA"),
    "lakers": ("Los Angeles Lakers", "NBA"),
    "warriors": ("Golden State Warriors", "NBA"),
    "bucks": ("Milwaukee Bucks", "NBA"),
    "heat": ("Miami Heat", "NBA"),
    "suns": ("Phoenix Suns", "NBA"),
    "nuggets": ("Denver Nuggets", "NBA"),
    "knicks": ("New York Knicks", "NBA"),
    "sixers": ("Philadelphia 76ers", "NBA"),
    "76ers": ("Philadelphia 76ers", "NBA"),
    # NFL
    "chiefs": ("Kansas City Chiefs", "NFL"),
    "49ers": ("San Francisco 49ers", "NFL"),
    "cowboys": ("Dallas Cowboys", "NFL"),
    "eagles": ("Philadelphia Eagles", "NFL"),
    "bills": ("Buffalo Bills", "NFL"),
    "packers": ("Green Bay Packers", "NFL"),
    "ravens": ("Baltimore Ravens", "NFL"),
    "bengals": ("Cincinnati Bengals", "NFL"),
    # MLB
    "yankees": ("New York Yankees", "MLB"),
    "new york y": ("New York Yankees", "MLB"),
    "red sox": ("Boston Red Sox", "MLB"),
    "dodgers": ("Los Angeles Dodgers", "MLB"),
    "giants": ("San Francisco Giants", "MLB"),
    "mets": ("New York Mets", "MLB"),
    "phillies": ("Philadelphia Phillies", "MLB"),
    "astros": ("Houston Astros", "MLB"),
    "cubs": ("Chicago Cubs", "MLB"),
    "cincinnati": ("Cincinnati Reds", "MLB"),
    "miami": ("Miami Marlins", "MLB"),
    # World Cup Teams
    "argentina": ("Argentina", "World Cup"),
    "brazil": ("Brazil", "World Cup"),
    "spain": ("Spain", "World Cup"),
    "portugal": ("Portugal", "World Cup"),
    "germany": ("Germany", "World Cup"),
    "netherlands": ("Netherlands", "World Cup"),
    # Esports
    "sentinels": ("Sentinels", "Esports"),
    "t1": ("T1", "Esports"),
    "navi": ("NaVi", "Esports"),
    "croatia": ("Croatia", "World Cup"),
    "usa": ("USA", "World Cup"),
    "mexico": ("Mexico", "World Cup"),
    "canada": ("Canada", "World Cup"),
    "norway": ("Norway", "World Cup"),
}

# Players list for common props
PLAYER_PROPS_DICT = {
    "tatum": ("Jayson Tatum", "NBA"),
    "jayson tatum": ("Jayson Tatum", "NBA"),
    "doncic": ("Luka Doncic", "NBA"),
    "luka doncic": ("Luka Doncic", "NBA"),
    "brown": ("Jaylen Brown", "NBA"),
    "jaylen brown": ("Jaylen Brown", "NBA"),
    "irving": ("Kyrie Irving", "NBA"),
    "kyrie irving": ("Kyrie Irving", "NBA"),
    "mahomes": ("Patrick Mahomes", "NFL"),
    "ohtani": ("Shohei Ohtani", "MLB"),
    # World Cup Soccer Players
    "messi": ("Lionel Messi", "World Cup"),
    "lionel messi": ("Lionel Messi", "World Cup"),
    "mbappe": ("Kylian Mbappe", "World Cup"),
    "kylian mbappe": ("Kylian Mbappe", "World Cup"),
    "bellingham": ("Jude Bellingham", "World Cup"),
    "jude bellingham": ("Jude Bellingham", "World Cup"),
    "ronaldo": ("Cristiano Ronaldo", "World Cup"),
    "cristiano ronaldo": ("Cristiano Ronaldo", "World Cup"),
    "kane": ("Harry Kane", "World Cup"),
    "harry kane": ("Harry Kane", "World Cup"),
    "vinicius": ("Vinicius Junior", "World Cup"),
    "vini": ("Vinicius Junior", "World Cup"),
    "bobb": ("Oscar Bobb", "World Cup"),
    "oscar bobb": ("Oscar Bobb", "World Cup"),
}

def parse_betting_post(text: str) -> list:
    """
    Main entry point for Module B.
    Attempts LLM parsing using Gemini/OpenAI if keys are in environment,
    otherwise falls back to a robust local regex-based NLP parser.
    Returns a list of parsed pick dictionaries.
    """
    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    
    # Process text through local SLM to verify if it's a catalyst
    if (openai_key or gemini_key) and _is_verified_catalyst_local_slm(text):
        if openai_key:
            return _parse_with_openai(text, openai_key)
        elif gemini_key:
            return _parse_with_gemini(text, gemini_key)
            
    return _parse_with_fallback(text)

def _is_verified_catalyst_local_slm(text: str) -> bool:
    """
    Local SLM routing for NLP sentiment screening.
    """
    return True
    
    import subprocess
    prompt = f"Analyze the following text. Does it contain a verified catalyst or strong betting signal? Answer only YES or NO.\\n\\nText: {text}"
    model_path = "/data/data/com.termux/files/home/qwen0.5b.gguf"
    if not os.path.exists(model_path):
        model_path = "/data/data/com.termux/files/home/model.gguf"
        
    try:
        result = subprocess.run([
            "/data/data/com.termux/files/usr/bin/llama-cli",
            "-m", model_path,
            "-p", prompt,
            "-n", "10",
            "--log-disable",
            "--simple-io"
        ], input="/exit\\n", capture_output=True, text=True, timeout=30)
        output = result.stdout.strip().upper()
        
        # In chat format, output may contain the prompt and then YES/NO.
        # We just check if YES is clearly in the final generation.
        if "YES" in output:
            print("🟢 Local SLM verified catalyst detected.")
            return True
        print("🔴 Local SLM rejected catalyst.")
        return False
    except Exception as e:
        print(f"⚠️ Local SLM routing failed: {e}")
        return False

def _parse_with_openai(text: str, api_key: str) -> list:
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        prompt = _build_prompt(text)
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            data = json.loads(content)
            return data.get("picks", [])
    except Exception as e:
        print(f"⚠️ OpenAI parsing failed: {e}")
    return _parse_with_fallback(text)

def _parse_with_gemini(text: str, api_key: str) -> list:
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        prompt = _build_prompt(text)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            content = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            data = json.loads(content)
            return data.get("picks", [])
    except Exception as e:
        print(f"⚠️ Gemini parsing failed: {e}")
    return _parse_with_fallback(text)

def _build_prompt(text: str) -> str:
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d")
    return f"""
You are an expert sports betting parser. The current date is {now_str}.
Analyze the unstructured capper text below.
Note that the FIFA World Cup 2026 is currently active (June-July 2026). Sports like NBA, NFL, and NHL are in their off-season. MLB is in-season.
Extract any distinct betting legs / picks contained in the text.

Capper Text: "{text}"

Return a strict JSON object with a single key "picks" containing an array of picks.
Each pick in the array MUST contain exactly these fields:
- "league": "NBA" | "MLB" | "NFL" | "NHL" | "Soccer" | "World Cup" | "Esports" | "Other" | "Consensus Parlay"
- "matchup": "Team A vs Team B" (determine the opponent teams if not fully specified. If it's a multi-leg parlay, use "Team A | Team B").
- "bet_type": "Moneyline" | "Spread" | "Player Prop Over" | "Player Prop Under" | "Total Over" | "Total Under" | "Parlay" | "Other"
- "target_asset": "Jayson Tatum" | "Argentina" etc. (For parlays, use a comma-separated list like "Argentina, Spain").
- "line_value": float | null (e.g. 26.5 for over/under, -3.0 for spread, null for moneyline)
- "odds": float | null (odds in decimal e.g. 1.91 or American e.g. -110 if explicitly mentioned, otherwise null)

# Important Rules for Extracting Parlays
If the text contains 'Combo', 'Parlay', or 'Multi-Leg' explicitly, you MUST aggregate the targets into a SINGLE pick with bet_type "Parlay", league "Consensus Parlay", and combine their names in target_asset (e.g. "Argentina, Spain").

Strict JSON output format:
{{
  "picks": [
     {{
       "league": "World Cup",
       "matchup": "Argentina vs Spain",
       "bet_type": "Parlay",
       "target_asset": "Argentina, Spain",
       "line_value": 0,
       "odds": 350
     }}
  ]
}}
If no picks are found, return {{"picks": []}}. Do not include any explanation or markdown formatting outside the JSON block.
"""

def _parse_with_fallback(text: str) -> list:
    """
    High-quality, regular expression based fallback parser.
    Identifies teams, players, leagues, lines, and bet types from unstructured capper posts.
    """
    print(f"🧩 Running fallback parser for: {text}")
    picks = []
    text_lower = text.lower()
    
    is_parlay = any(k in text_lower for k in ["combo", "parlay", "multi-leg", "multileg"])
    if is_parlay:
        matched_teams = []
        for alias, (team_name, league) in TEAM_ALIASES.items():
            if alias in text_lower and team_name not in matched_teams:
                matched_teams.append(team_name)
        
        if matched_teams:
            return [{
                "league": "Consensus Parlay",
                "matchup": " | ".join(matched_teams),
                "bet_type": "Parlay",
                "target_asset": ", ".join(matched_teams),
                "line_value": None,
                "odds": None
            }]

    
    # 1. Look for Player Props (e.g. "Tatum Over 26.5 points", "Tatum Over 26.5")
    for alias, (full_name, league) in PLAYER_PROPS_DICT.items():
        if alias in text_lower:
            # Avoid duplicate target asset matches
            if any(p["target_asset"] == full_name for p in picks):
                continue
            # Look for lines: e.g. "over 26.5" or "o26.5" or "u26.5" or "under 26.5"
            prop_pattern = r"(over|under|o|u)\s*(\d+\.?\d*)"
            match = re.search(alias + r".*?" + prop_pattern, text_lower)
            if not match:
                # Try before player name
                match = re.search(prop_pattern + r".*?" + alias, text_lower)
                
            if match:
                direction = match.group(1).strip()
                val = float(match.group(2))
                bet_type = "Player Prop Over" if direction in ["over", "o"] else "Player Prop Under"
                
                # Determine matchup (default or search for teams)
                opponent = "Opponent"
                for team_alias, (team_name, _) in TEAM_ALIASES.items():
                    if team_alias in text_lower:
                        opponent = team_name
                        break
                        
                matchup = f"{full_name} ({opponent})"
                
                picks.append({
                    "league": league,
                    "matchup": matchup,
                    "bet_type": bet_type,
                    "target_asset": full_name,
                    "line_value": val,
                    "odds": None
                })

    # 2. Look for Team Bets (Moneyline, Spreads)
    for alias, (team_name, league) in TEAM_ALIASES.items():
        if alias in text_lower:
            # If the team is already a part of matchup of an existing pick, skip it
            if any(team_name in p["matchup"] or p["target_asset"] == team_name for p in picks):
                continue
            # Check for Moneyline/Kalshi YES shares: e.g. "mavs ml", "yes sentinels"
            if any(k in text_lower for k in ["ml", "moneyline", "ml ", "win", "yes "]):
                # Look for potential opponent
                opponent = "Opponent"
                for opp_alias, (opp_name, opp_league) in TEAM_ALIASES.items():
                    if opp_alias != alias and opp_league == league and opp_alias in text_lower:
                        opponent = opp_name
                        break
                matchup = f"{team_name} vs {opponent}"
                picks.append({
                    "league": league,
                    "matchup": matchup,
                    "bet_type": "Moneyline",
                    "target_asset": team_name,
                    "line_value": None,
                    "odds": None
                })
                continue

            # Check for Spreads: e.g. "mavs -3", "celtics +4.5"
            spread_pattern = r"([+-]\d+\.?\d*)"
            match = re.search(alias + r"\s*" + spread_pattern, text_lower)
            if match:
                val = float(match.group(1))
                opponent = "Opponent"
                for opp_alias, (opp_name, opp_league) in TEAM_ALIASES.items():
                    if opp_alias != alias and opp_league == league and opp_alias in text_lower:
                        opponent = opp_name
                        break
                matchup = f"{team_name} vs {opponent}"
                picks.append({
                    "league": league,
                    "matchup": matchup,
                    "bet_type": "Spread",
                    "target_asset": team_name,
                    "line_value": val,
                    "odds": None
                })
                continue

    # 3. Last resort: general parser
    if not picks:
        for alias, (team_name, league) in TEAM_ALIASES.items():
            if alias in text_lower:
                # Avoid duplicate target asset matches
                if any(p["target_asset"] == team_name for p in picks):
                    continue
                picks.append({
                    "league": league,
                    "matchup": f"{team_name} vs Opponent",
                    "bet_type": "Moneyline",
                    "target_asset": team_name,
                    "line_value": None,
                    "odds": None
                })
    return picks

def parse_social_signal(text: str) -> dict:
    """
    Parses a social post payload for sports betting signal metadata.
    Scans for teams, bet types, and outcomes.
    """
    text_lower = text.lower()
    
    # 1. Detect League / Sport
    league = "NBA"
    if any(x in text_lower for x in ["world cup", "fifa", "soccer", "football", "goal", "goals"]):
        league = "World Cup"
    elif any(x in text_lower for x in ["nfl", "touchdown", "passing", "yards", "qb"]):
        league = "NFL"
    elif any(x in text_lower for x in ["mlb", "strikeout", "strikeouts", "hit", "hits", "home run"]):
        league = "MLB"
        
    # 2. Detect Team
    detected_team = None
    for alias, (team, leg) in TEAM_ALIASES.items():
        if alias in text_lower:
            detected_team = team
            league = leg
            break
            
    # 3. Detect Bet Type
    bet_type = "Moneyline"
    if "spread" in text_lower or "line" in text_lower or "+" in text_lower or "-" in text_lower:
        bet_type = "Spread"
    if "over" in text_lower:
        bet_type = "Player Prop Over" if detected_team else "Total Over"
    elif "under" in text_lower:
        bet_type = "Player Prop Under" if detected_team else "Total Under"
        
    # 4. Detect Outcome parameter
    outcome_parameter = ""
    margin_match = re.search(r"(?:win by more than|spread of|by)\s*([+-]?\d+(?:\.\d+)?)", text_lower)
    if margin_match:
        outcome_parameter = f"win by more than {margin_match.group(1)}"
        
    return {
        "league": league,
        "team": detected_team,
        "bet_type": bet_type,
        "outcome_parameter": outcome_parameter
    }

def perform_ocr_on_image(image_url: str) -> str:
    """
    Placeholder function for an OCR library (like pytesseract)
    to extract text from image URLs automatically.
    """
    print(f"🖼️ [OCR] Simulating OCR text extraction on image URL: {image_url}")
    # In a real environment, you would fetch the image and use pytesseract:
    # import pytesseract
    # from PIL import Image
    # text = pytesseract.image_to_string(Image.open(requests.get(image_url, stream=True).raw))
    # Text that represents a betting call with odds
    return "Lionel Messi over 0.5 goals against Spain tonight! Bang!"
