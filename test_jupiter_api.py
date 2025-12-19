#!/usr/bin/env python3
"""
Script test untuk melihat hasil dari Jupiter API
"""

import asyncio
import aiohttp
import json
import os

JUPITER_API_KEY = "efd896ec-30ed-4c89-a990-32b315e13d20"
BOT_CALL_MIN_MARKET_CAP = 200000  # 200k USD
BOT_CALL_MIN_FEES_SOL = 20  # 20 SOL

def _format_usd(value):
    if not value or value <= 0:
        return "N/A"
    thresholds = [
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ]
    for threshold, suffix in thresholds:
        if value >= threshold:
            return f"${value/threshold:.2f}{suffix}"
    return f"${value:,.0f}"

async def test_jupiter_api():
    """Test Jupiter API untuk melihat token yang akan dikirim"""
    async with aiohttp.ClientSession() as session:
        try:
            url = "https://api.jup.ag/tokens/v2/recent"
            headers = {
                "x-api-key": JUPITER_API_KEY
            }
            
            print("=" * 80)
            print("FETCHING TOKENS FROM JUPITER API...")
            print("=" * 80)
            print(f"URL: {url}")
            print(f"API Key: {JUPITER_API_KEY[:20]}...")
            print()
            
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                print(f"Status Code: {response.status}")
                
                if response.status == 429:
                    print("❌ Rate limited!")
                    return
                
                response.raise_for_status()
                data = await response.json()
                
                # Check structure
                print(f"\nResponse Type: {type(data)}")
                if isinstance(data, list):
                    print(f"Number of tokens: {len(data)}")
                    if len(data) > 0:
                        print(f"\nFirst token structure:")
                        print(json.dumps(data[0], indent=2))
                else:
                    print(f"Response structure:")
                    print(json.dumps(data, indent=2)[:1000])
                
                # Get SOL price
                sol_price_usd = 125.0
                try:
                    price_url = "https://price.jup.ag/v4/price?ids=SOL"
                    async with session.get(price_url, timeout=aiohttp.ClientTimeout(total=10)) as price_resp:
                        if price_resp.status == 200:
                            price_data = await price_resp.json()
                            if "data" in price_data and "SOL" in price_data["data"]:
                                sol_price_usd = float(price_data["data"]["SOL"].get("price", 125.0))
                                print(f"\n✅ SOL Price: ${sol_price_usd:.2f}")
                except Exception as e:
                    print(f"\n⚠️ Could not fetch SOL price: {e}, using default ${sol_price_usd}")
                
                # Process tokens
                if isinstance(data, list):
                    print("\n" + "=" * 80)
                    print("PROCESSING TOKENS...")
                    print("=" * 80)
                    print(f"Criteria:")
                    print(f"  • Market Cap >= ${BOT_CALL_MIN_MARKET_CAP:,.0f}")
                    print(f"  • Total Fees >= {BOT_CALL_MIN_FEES_SOL} SOL")
                    print()
                    
                    qualifying = []
                    for idx, token in enumerate(data[:20], 1):  # Check first 20
                        try:
                            address = token.get("address", "")
                            name = token.get("name", "Unknown")
                            symbol = token.get("symbol", "UNKNOWN")
                            
                            market_cap = None
                            if "marketCap" in token:
                                try:
                                    market_cap = float(token["marketCap"])
                                except:
                                    pass
                            
                            volume_24h = None
                            if "volume24h" in token:
                                try:
                                    volume_24h = float(token["volume24h"])
                                except:
                                    pass
                            
                            fee_percentage = 0.003
                            total_fees_usd = volume_24h * fee_percentage if volume_24h else 0
                            total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
                            
                            market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP
                            fees_ok = total_fees_sol >= BOT_CALL_MIN_FEES_SOL
                            
                            print(f"[{idx}] {symbol} - {name[:40]}")
                            print(f"     Market Cap: {_format_usd(market_cap) if market_cap else 'N/A'} {'✅' if market_cap_ok else '❌'}")
                            print(f"     Fees: {total_fees_sol:.2f} SOL ({_format_usd(total_fees_usd)}) {'✅' if fees_ok else '❌'}")
                            print(f"     Volume 24h: {_format_usd(volume_24h)}")
                            
                            if market_cap_ok and fees_ok:
                                qualifying.append({
                                    "symbol": symbol,
                                    "name": name,
                                    "address": address,
                                    "market_cap": market_cap,
                                    "fees_sol": total_fees_sol,
                                })
                                print(f"     ✅ QUALIFIED!")
                            print()
                            
                        except Exception as e:
                            print(f"[{idx}] Error: {e}")
                            continue
                    
                    print("=" * 80)
                    print("RESULTS")
                    print("=" * 80)
                    print(f"\n✅ Found {len(qualifying)} qualifying token(s)")
                    
                    if qualifying:
                        # Calculate scores
                        def calculate_score(token):
                            market_cap = token.get("market_cap", 0) or 0
                            fees_sol = token.get("fees_sol", 0) or 0
                            market_cap_score = (market_cap / 1_000_000) * 0.6
                            fees_score = (fees_sol / 100) * 0.4
                            return market_cap_score + fees_score
                        
                        qualifying.sort(key=calculate_score, reverse=True)
                        best = qualifying[0]
                        
                        print(f"\n🏆 BEST TOKEN (WILL BE SENT):")
                        print(f"   Symbol: {best['symbol']}")
                        print(f"   Name: {best['name']}")
                        print(f"   Address: {best['address']}")
                        print(f"   Market Cap: {_format_usd(best['market_cap'])}")
                        print(f"   Fees: {best['fees_sol']:.2f} SOL")
                        print(f"   Score: {calculate_score(best):.4f}")
                    else:
                        print("\n❌ No tokens meet the criteria")
                
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_jupiter_api())


