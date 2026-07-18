"""
ai_predictive_model.py — BullX & NEO Inspired Predictive AI Scoring & Infrastructure Optimizer
Calculates token success/health scores and optimizes gas/RPC routing.
"""
import os
import json
import time
import requests

def get_base_predictive_score(token_addr, lp_lock_pct, goplus_data):
    """
    NEO/BullX-inspired predictive model for EVM (Base).
    Calculates success score (0-100) based on contract, lp, tax, and centralization.
    """
    score = 0
    checks = {}
    
    # 1. Contract Open Source status (+15 pts)
    if goplus_data.get("is_open_source") == "1":
        score += 15
        checks["Verified Contract"] = "Passed (+15)"
    else:
        checks["Verified Contract"] = "Failed (+0)"
        
    # 2. Renounced Ownership status (+15 pts)
    owner = goplus_data.get("owner_address", "")
    if not owner or owner.lower() in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]:
        score += 15
        checks["Renounced Ownership"] = "Passed (+15)"
    else:
        checks["Renounced Ownership"] = f"Active Owner ({owner[:8]}...) (+0)"
        
    # 3. Tax Check (+15 pts max)
    buy_tax = float(goplus_data.get("buy_tax", 0) or 0)
    sell_tax = float(goplus_data.get("sell_tax", 0) or 0)
    if buy_tax == 0 and sell_tax == 0:
        score += 15
        checks["Buy/Sell Tax"] = "Passed (0% / 0% Tax) (+15)"
    elif buy_tax <= 0.03 and sell_tax <= 0.03:
        score += 10
        checks["Buy/Sell Tax"] = f"Low Tax ({buy_tax:.1%} / {sell_tax:.1%}) (+10)"
    elif buy_tax <= 0.08 and sell_tax <= 0.08:
        score += 5
        checks["Buy/Sell Tax"] = f"Med Tax ({buy_tax:.1%} / {sell_tax:.1%}) (+5)"
    else:
        checks["Buy/Sell Tax"] = f"High Tax ({buy_tax:.1%} / {sell_tax:.1%}) (+0)"
        
    # 4. LP Lock status (+25 pts max)
    if lp_lock_pct >= 0.99:
        score += 25
        checks["Liquidity Lock"] = f"Excellent ({lp_lock_pct * 100:.1f}%) (+25)"
    elif lp_lock_pct >= 0.95:
        score += 20
        checks["Liquidity Lock"] = f"Good ({lp_lock_pct * 100:.1f}%) (+20)"
    elif lp_lock_pct >= 0.85:
        score += 15
        checks["Liquidity Lock"] = f"Fair ({lp_lock_pct * 100:.1f}%) (+15)"
    elif lp_lock_pct >= 0.80:
        score += 10
        checks["Liquidity Lock"] = f"Min Requirement ({lp_lock_pct * 100:.1f}%) (+10)"
    else:
        checks["Liquidity Lock"] = f"Insufficient ({lp_lock_pct * 100:.1f}%) (+0)"
        
    # 5. Distribution centralization (+30 pts max)
    top_10_share = float(goplus_data.get("top_10_holders_share", 1.0) or 1.0)
    if top_10_share <= 0.15:
        score += 30
        checks["Whale/Cabal centralization"] = f"Extremely Decentralized ({top_10_share:.1%}) (+30)"
    elif top_10_share <= 0.25:
        score += 20
        checks["Whale/Cabal centralization"] = f"Decentralized ({top_10_share:.1%}) (+20)"
    elif top_10_share <= 0.35:
        score += 10
        checks["Whale/Cabal centralization"] = f"Normal Distribution ({top_10_share:.1%}) (+10)"
    elif top_10_share <= 0.45:
        score += 5
        checks["Whale/Cabal centralization"] = f"Borderline centralized ({top_10_share:.1%}) (+5)"
    else:
        checks["Whale/Cabal centralization"] = f"Highly centralized ({top_10_share:.1%}) (+0)"
        
    # Red Flags / Deductions
    if goplus_data.get("is_mintable") == "1" and owner:
        score -= 25
        checks["Red Flag: Mintable"] = "Active owner can mint tokens (-25)"
        
    if goplus_data.get("slippage_modifiable") == "1" or goplus_data.get("personal_slippage_modifiable") == "1":
        score -= 30
        checks["Red Flag: Modifiable Taxes"] = "Creator can honeypot token dynamically (-30)"
        
    if goplus_data.get("is_blacklisted") == "1":
        score -= 20
        checks["Red Flag: Address Freezing"] = "Creator can freeze holder wallets (-20)"
        
    score = max(0, min(100, score))
    return score, checks


def get_solana_predictive_score(token_addr, rugcheck_report, mint_auth_revoked, freeze_auth_revoked):
    """
    NEO/BullX-inspired predictive model for Solana.
    Calculates success score (0-100) based on rugcheck score, freeze/mint authorities,
    and metadata availability.
    """
    score = 0
    checks = {}
    
    # 1. Mint authority (+25 pts)
    if mint_auth_revoked:
        score += 25
        checks["Mint Authority"] = "Revoked (+25)"
    else:
        checks["Mint Authority"] = "Active (Dev can print and dump) (+0)"
        
    # 2. Freeze authority (+25 pts)
    if freeze_auth_revoked:
        score += 25
        checks["Freeze Authority"] = "Revoked (+25)"
    else:
        checks["Freeze Authority"] = "Active (Dev can honeypot) (+0)"
        
    # 3. RugCheck Score (+30 pts max)
    if rugcheck_report:
        rc_score = rugcheck_report.get("score", 0)
        if rc_score == 0:
            score += 30
            checks["RugCheck Security Index"] = "Perfect (Score: 0) (+30)"
        elif rc_score < 300:
            score += 25
            checks["RugCheck Security Index"] = f"Low Risk (Score: {rc_score}) (+25)"
        elif rc_score < 600:
            score += 15
            checks["RugCheck Security Index"] = f"Med Risk (Score: {rc_score}) (+15)"
        elif rc_score <= 1200:
            score += 5
            checks["RugCheck Security Index"] = f"High Risk (Score: {rc_score}) (+5)"
        else:
            checks["RugCheck Security Index"] = f"Critical Risk (Score: {rc_score}) (+0)"
    else:
        if mint_auth_revoked and freeze_auth_revoked:
            score += 20
            checks["RugCheck Security Index"] = "Pending Indexing (Assumed Low Risk via On-Chain Revocations) (+20)"
        else:
            checks["RugCheck Security Index"] = "Pending Indexing (Unverified Risk) (+0)"
        
    # 4. Social & Metadata verification (+20 pts max)
    has_socials = False
    if rugcheck_report:
        file_meta = rugcheck_report.get("file_meta", {})
        if file_meta.get("external_url") or file_meta.get("description"):
            has_socials = True
            
    if has_socials:
        score += 20
        checks["Social Metadata Presence"] = "Passed (Verified Website/Links) (+20)"
    else:
        if not rugcheck_report and mint_auth_revoked and freeze_auth_revoked:
            score += 10
            checks["Social Metadata Presence"] = "Pending Indexing (Default Neutral) (+10)"
        else:
            checks["Social Metadata Presence"] = "Failed (No social links in token info) (+0)"
        
    if rugcheck_report:
        risks = rugcheck_report.get("risks", [])
        danger_count = sum(1 for r in risks if r.get("level") == "danger")
        warn_count = sum(1 for r in risks if r.get("level") == "warn")
        
        if danger_count > 0:
            score -= (danger_count * 20)
            checks["Red Flag: Critical Risks"] = f"Danger risks: {danger_count} flags (-{danger_count*20})"
        if warn_count > 0:
            score -= (warn_count * 5)
            checks["RugCheck Warnings"] = f"Warnings: {warn_count} flags (-{warn_count*5})"
            
    score = max(0, min(100, score))
    return score, checks

# ── Infrastructure Optimizer ──────────────────────────────────────────────────

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58decode(v: str) -> bytes:
    n = 0
    for char in v:
        n = n * 58 + BASE58_ALPHABET.index(char)
    res = bytearray()
    while n > 0:
        n, r = divmod(n, 256)
        res.append(r)
    pad = 0
    for char in v:
        if char == "1":
            pad += 1
        else:
            break
    return bytes([0] * pad + list(reversed(res)))

def parse_compact_u16(b: bytes, offset: int) -> tuple:
    value = 0
    shift = 0
    while True:
        byte = b[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return value, offset

def optimize_solana_compute_budget(swap_transaction_b64):
    """
    NEO-inspired Compute Unit (CU) optimizer.
    Parses the transaction, finds the Compute Budget instruction,
    and adds a dynamic 15% safety buffer to limits to prevent execution timeout fails.
    """
    try:
        import base64
        tx_bytes = bytearray(base64.b64decode(swap_transaction_b64))
        
        offset = 0
        
        # 1. Read signatures length
        num_sigs, offset = parse_compact_u16(tx_bytes, offset)
        # Skip signatures
        offset += num_sigs * 64
        
        # Start of message
        # Read header
        offset += 3
        
        # Read account keys length
        num_accounts, offset = parse_compact_u16(tx_bytes, offset)
        
        # Read account keys and find ComputeBudget program index
        COMPUTE_BUDGET_PROGRAM_BYTES = _b58decode("ComputeBudget111111111111111111111111111111")
        compute_budget_index = -1
        for idx in range(num_accounts):
            key_bytes = tx_bytes[offset:offset+32]
            if key_bytes == COMPUTE_BUDGET_PROGRAM_BYTES:
                compute_budget_index = idx
            offset += 32
            
        if compute_budget_index == -1:
            # ComputeBudget program not referenced in accounts
            return swap_transaction_b64
            
        # Skip recent blockhash
        offset += 32
        
        # Read instructions length
        num_instructions, offset = parse_compact_u16(tx_bytes, offset)
        
        # Parse each instruction
        for _ in range(num_instructions):
            program_id_index = tx_bytes[offset]
            offset += 1
            
            # Read accounts length
            num_inst_accounts, offset = parse_compact_u16(tx_bytes, offset)
            # Skip accounts
            offset += num_inst_accounts
            
            # Read data length
            data_len, offset = parse_compact_u16(tx_bytes, offset)
            
            # If this is a ComputeBudget instruction
            if program_id_index == compute_budget_index:
                # Instruction data
                inst_data = tx_bytes[offset:offset+data_len]
                # Look for SetComputeUnitLimit instruction index (0x02)
                if len(inst_data) == 5 and inst_data[0] == 0x02:
                    current_limit = int.from_bytes(inst_data[1:5], "little")
                    new_limit = int(current_limit * 1.15)
                    new_limit = min(new_limit, 1400000) # Cap at 1.4M CUs
                    # Update bytes in place
                    tx_bytes[offset+1:offset+5] = new_limit.to_bytes(4, "little")
                    print(f"⚡ [INFRASTRUCTURE OPTIMIZER] Compute Unit Limit optimized: {current_limit} -> {new_limit} (+15% safety buffer)")
                    
            offset += data_len
            
        return base64.b64encode(tx_bytes).decode("utf-8")
    except Exception as e:
        print(f"⚠️ Failed to optimize Compute Budget: {e}")
        return swap_transaction_b64

def predict(token_addr: str) -> float:
    """
    Predicts the safety/success probability of a token (0.0 to 1.0).
    Uses Gemini API for advanced threat analysis, falling back to heuristics if offline.
    """
    is_evm = token_addr.startswith("0x")
    raw_data = {}
    
    if is_evm:
        url = f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={token_addr}"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                raw_data = r.json().get("result", {}).get(token_addr.lower(), {})
        except Exception:
            pass
    else:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_addr}/report"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                raw_data = r.json()
        except Exception:
            pass

    # Try Gemini LLM first if API key is present
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.5-flash")
            
            prompt = (
                f"You are a NEO/BullX-inspired predictive trading AI model.\n"
                f"Evaluate the security telemetry data below for the token: {token_addr}\n"
                f"Network: {'Base (EVM)' if is_evm else 'Solana'}\n"
                f"Data:\n{json.dumps(raw_data, indent=2)[:3000]}\n\n"
                f"Identify honeypots, developer balance manipulations, mint backdoors, high taxes, and centralization.\n"
                f"Return ONLY a single floating-point number between 0.0 and 1.0 representing the safety/success probability (e.g. 0.85). Do not include any other text."
            )
            response = model.generate_content(prompt)
            result_str = response.text.strip()
            
            import re
            match = re.search(r"\d+\.\d+", result_str)
            if match:
                pred = float(match.group())
                if 0.0 <= pred <= 1.0:
                    print(f"🧠 [GEMINI PREDICTIVE BRAIN] Evaluated {token_addr[:12]} -> {pred:.4f}")
                    return pred
        except Exception as e:
            print(f"⚠️ Gemini prediction failed, falling back to heuristics: {e}")

    # Fallback to local scoring algorithms
    if is_evm:
        lp_lock_pct = 0.95
        score, _ = get_base_predictive_score(token_addr, lp_lock_pct, raw_data)
        pred = score / 100.0
    else:
        score, _ = get_solana_predictive_score(token_addr, raw_data, True, True)
        pred = score / 100.0
        
    print(f"🧮 [HEURISTIC BRAIN] Evaluated {token_addr[:12]} -> {pred:.4f}")
    return pred

def predict_kalshi_market(ticker: str, current_prob: float) -> float:
    import random
    boost = 0.0
    ticker_lower = ticker.lower()
    if any(k in ticker_lower for k in ["trump", "election", "biden"]):
        boost = 0.05
    elif any(k in ticker_lower for k in ["fed", "rate", "inflation"]):
        boost = 0.03
    noise = random.uniform(-0.01, 0.02)
    enhanced_prob = current_prob + boost + noise
    return max(0.01, min(0.99, enhanced_prob))
