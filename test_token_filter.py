#!/usr/bin/env python3
"""
Test script untuk cek kenapa token tidak ter-notifikasi
"""
import requests
import json
from datetime import datetime

# Config
JUPITER_API_KEY = "efd896ec-30ed-4c89-a990-32b315e13d20"
BOT_CALL_MIN_MARKET_CAP = 250000
BOT_CALL_MAX_MARKET_CAP = 10000000
BOT_CALL_MIN_FEES_SOL = 20
BOT_CALL_MIN_PRICE_CHANGE_1H = 20

# Token yang mau di-test
TEST_TOKENS = [
    "3k29upUrDXNF3cuRYArqUKw8AtUNWSqbfZfRvB6fBAGS",  # SHIRLEY
    "GLBV9FAMhULQpD6iQMGBSchD9s1Hdzd79VqetVjgpump",  # SilverWhale
]

def fetch_sol_price():
    """Fetch SOL price"""
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return float(data["solana"]["usd"])
    except:
        pass
    return 125.0  # default

def test_jupiter_api():
    """Test Jupiter API toptraded endpoint"""
    print("=" * 80)
    print("TEST 1: Jupiter API toptraded/1h endpoint")
    print("=" * 80)
    
    url = f"https://api.jup.ag/tokens/v2/toptraded/1h?limit=100&minMcap={int(BOT_CALL_MIN_MARKET_CAP)}&maxMcap={int(BOT_CALL_MAX_MARKET_CAP)}"
    headers = {
        "x-api-key": JUPITER_API_KEY
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"ERROR: {response.text}")
            return []
        
        data = response.json()
        tokens = data if isinstance(data, list) else []
        print(f"Total tokens returned: {len(tokens)}")
        
        # Check if test tokens are in response
        print("\nChecking if test tokens are in response:")
        for test_token in TEST_TOKENS:
            found = False
            for token in tokens:
                token_id = token.get("id") or token.get("address", "")
                if token_id == test_token:
                    found = True
                    print(f"  ✅ {test_token[:8]}... FOUND!")
                    print(f"     Symbol: {token.get('symbol', 'N/A')}")
                    print(f"     Name: {token.get('name', 'N/A')}")
                    break
            if not found:
                print(f"  ❌ {test_token[:8]}... NOT FOUND in response")
        
        return tokens
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []

def test_token_filter(token_data):
    """Test filter logic untuk token tertentu"""
    print("\n" + "=" * 80)
    print(f"TEST 2: Filter Logic untuk token")
    print("=" * 80)
    
    token_address = token_data.get("id") or token_data.get("address", "N/A")
    token_symbol = token_data.get("symbol", "UNKNOWN")
    token_name = token_data.get("name", "Unknown")
    
    print(f"Token: {token_symbol} ({token_name})")
    print(f"Address: {token_address}")
    
    # Get market cap
    market_cap = None
    if "mcap" in token_data:
        try:
            market_cap = float(token_data["mcap"])
        except:
            pass
    if not market_cap and "fdv" in token_data:
        try:
            market_cap = float(token_data["fdv"])
        except:
            pass
    if not market_cap and "marketCap" in token_data:
        try:
            market_cap = float(token_data["marketCap"])
        except:
            pass
    
    print(f"\n1. Market Cap Check:")
    mcap_str = f"${market_cap:,.0f}" if market_cap else "$0"
    print(f"   Market Cap: {mcap_str}")
    print(f"   Min: ${BOT_CALL_MIN_MARKET_CAP:,.0f}")
    print(f"   Max: ${BOT_CALL_MAX_MARKET_CAP:,.0f}")
    market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP and market_cap <= BOT_CALL_MAX_MARKET_CAP
    print(f"   Result: {'✅ PASS' if market_cap_ok else '❌ FAIL'}")
    
    # Get volume 24h
    volume_24h_usd = None
    stats24h = token_data.get("stats24h", {})
    stats1h = token_data.get("stats1h", {})
    if isinstance(stats24h, dict):
        buy_volume = stats24h.get("buyVolume", 0) or 0
        sell_volume = stats24h.get("sellVolume", 0) or 0
        try:
            volume_24h_usd = float(buy_volume) + float(sell_volume)
        except:
            pass
    
    # Get price change 1h
    price_change_1h = None
    if stats1h and isinstance(stats1h, dict):
        price_change_1h = stats1h.get("priceChange")
        if price_change_1h is not None:
            try:
                price_change_1h = float(price_change_1h)
            except:
                price_change_1h = None
    
    print(f"\n2. Price Change 1h Check:")
    price_change_str = f"{price_change_1h:.2f}%" if price_change_1h is not None else "N/A"
    print(f"   Price Change 1h: {price_change_str}")
    print(f"   Min Required: {BOT_CALL_MIN_PRICE_CHANGE_1H}%")
    price_change_1h_ok = price_change_1h is not None and price_change_1h >= BOT_CALL_MIN_PRICE_CHANGE_1H
    print(f"   Result: {'✅ PASS' if price_change_1h_ok else '❌ FAIL'}")
    
    # Calculate fees
    sol_price_usd = fetch_sol_price()
    print(f"\n3. Fees Check:")
    vol_str = f"${volume_24h_usd:,.0f}" if volume_24h_usd else "$0"
    print(f"   Volume 24h: {vol_str}")
    fee_percentage = 0.003
    total_fees_usd = volume_24h_usd * fee_percentage if volume_24h_usd else 0
    total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
    print(f"   Fees (0.3% of volume): ${total_fees_usd:,.2f} = {total_fees_sol:.2f} SOL")
    print(f"   Min Required: {BOT_CALL_MIN_FEES_SOL} SOL")
    fees_ok = total_fees_sol >= BOT_CALL_MIN_FEES_SOL
    print(f"   Result: {'✅ PASS' if fees_ok else '❌ FAIL'}")
    
    # Get created_at for reference only (tidak digunakan untuk filter)
    created_at = token_data.get("createdAt") or token_data.get("created_at") or token_data.get("firstPool", {}).get("createdAt")
    if created_at:
        try:
            if isinstance(created_at, str):
                if created_at.isdigit():
                    created_ts = int(created_at)
                else:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    created_ts = dt.timestamp()
            elif isinstance(created_at, (int, float)):
                created_ts = created_at if created_at > 1e10 else created_at * 1000
            else:
                created_ts = 0
            
            if created_ts > 0:
                created_ts_seconds = created_ts / 1000 if created_ts > 1e10 else created_ts
                now = datetime.now().timestamp()
                age_hours = (now - created_ts_seconds) / 3600
                print(f"\n4. Token Info (for reference only, tidak digunakan untuk filter):")
                print(f"   Created: {created_at}")
                print(f"   Age: {age_hours:.2f} hours")
                print(f"   Note: Token age TIDAK digunakan untuk filter (sudah dihapus)")
        except Exception as e:
            pass
    
    # Final result
    print(f"\n" + "=" * 80)
    print("FINAL RESULT:")
    print("=" * 80)
    all_ok = market_cap_ok and fees_ok and price_change_1h_ok
    print(f"Market Cap: {'✅' if market_cap_ok else '❌'}")
    print(f"Fees: {'✅' if fees_ok else '❌'}")
    print(f"Price Change 1h: {'✅' if price_change_1h_ok else '❌'}")
    print(f"\nOverall: {'✅ TOKEN QUALIFIES' if all_ok else '❌ TOKEN DOES NOT QUALIFY'}")
    
    if not all_ok:
        print("\nReasons for failure:")
        if not market_cap_ok:
            print("  - Market cap tidak memenuhi kriteria")
        if not fees_ok:
            print("  - Fees tidak memenuhi kriteria")
        if not price_change_1h_ok:
            print("  - Price change 1h tidak memenuhi kriteria")
    
    return all_ok

def main():
    print("Testing Bot Call Filter Logic")
    print("=" * 80)
    
    # Test 1: Fetch from Jupiter API
    tokens = test_jupiter_api()
    
    if not tokens:
        print("\n❌ No tokens returned from Jupiter API")
        return
    
    # Test 2: Check each test token
    for test_token in TEST_TOKENS:
        found_token = None
        for token in tokens:
            token_id = token.get("id") or token.get("address", "")
            if token_id == test_token:
                found_token = token
                break
        
        if found_token:
            test_token_filter(found_token)
        else:
            print(f"\n" + "=" * 80)
            print(f"Token {test_token[:8]}... NOT FOUND in Jupiter API response")
            print("=" * 80)
            print("Possible reasons:")
            print("  1. Token tidak masuk top traded 1h")
            print("  2. Token sudah terlalu lama (tidak aktif trading)")
            print("  3. Token tidak masuk range market cap yang di-filter oleh API")
    
    print("\n" + "=" * 80)
    print("Test completed!")
    print("=" * 80)

if __name__ == "__main__":
    main()

