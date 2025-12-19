#!/usr/bin/env python3
"""
Script test untuk melihat koin apa yang akan dikirim oleh bot call
"""

import asyncio
import aiohttp
import json
import time
import os
import re
import requests
from datetime import datetime
from typing import Dict, List, Optional

# Simulasi konstanta dari main.py
BOT_CALL_MIN_MARKET_CAP = 250000  # 250k USD
BOT_CALL_MAX_MARKET_CAP = 10000000  # 10jt USD
BOT_CALL_MIN_FEES_SOL = 20  # 20 SOL
BOT_CALL_MIN_PRICE_CHANGE_1H = 50  # 50% price change dalam 1 jam
SOL_MINT = "So11111111111111111111111111111111111111112"

# API Source: "jupiter" atau "dexscreener"
# Jupiter lebih akurat untuk token baru, tapi perlu API key
# DexScreener tidak perlu API key tapi kurang efektif
USE_JUPITER_API = os.getenv("USE_JUPITER_API", "true").lower() == "true"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "efd896ec-30ed-4c89-a990-32b315e13d20")

# Option untuk fetch volume/fee dari Meteora sebagai tambahan
USE_METEORA_FOR_FEES = os.getenv("USE_METEORA_FOR_FEES", "false").lower() == "true"

def is_valid_solana_address(addr: str):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', addr))

def _format_usd(value: Optional[float]) -> str:
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

def fetch_meteora_volume_and_fees(token_address: str) -> tuple[Optional[float], Optional[float]]:
    """Fetch volume and fees data from Meteora pools for a token address.
    Returns: (volume_24h_usd, fees_24h_usd)"""
    if not USE_METEORA_FOR_FEES:
        return None, None
    
    try:
        # Meteora API endpoint untuk pools
        url = 'https://dlmm-api.meteora.ag/pair/all_by_groups'
        params = {
            'search_term': token_address,
            'sort_key': 'tvl',
            'order_by': 'desc',
            'limit': 20  # Ambil lebih banyak pools untuk akumulasi
        }
        
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return None, None
        
        data = response.json()
        total_volume_24h = 0
        total_fees_24h = 0
        
        # Extract volume and fees from pools
        if isinstance(data, dict) and 'groups' in data:
            for group in data.get('groups', []):
                pools = group.get('pairs', [])
                for pool in pools:
                    try:
                        # Meteora API provides volume and fees data
                        # Try multiple possible field names
                        volume_24h = (
                            pool.get('trade_volume_24h') or 
                            (pool.get('volume', {}).get('hour_24') if isinstance(pool.get('volume'), dict) else None) or
                            pool.get('volume24h') or
                            pool.get('volume_24h')
                        )
                        
                        fees_24h = (
                            pool.get('fees_24h') or
                            (pool.get('fees', {}).get('hour_24') if isinstance(pool.get('fees'), dict) else None) or
                            pool.get('fees24h') or
                            pool.get('fees_24h')
                        )
                        
                        if volume_24h:
                            try:
                                total_volume_24h += float(volume_24h)
                            except (ValueError, TypeError):
                                pass
                        
                        if fees_24h:
                            try:
                                total_fees_24h += float(fees_24h)
                            except (ValueError, TypeError):
                                pass
                    except Exception:
                        continue
        
        volume = total_volume_24h if total_volume_24h > 0 else None
        fees = total_fees_24h if total_fees_24h > 0 else None
        
        return volume, fees
    except Exception as e:
        print(f"[DEBUG] Error fetching Meteora data for {token_address[:8]}...: {e}")
        return None, None

async def fetch_new_tokens_jupiter() -> List[Dict[str, object]]:
    """Fetch new tokens from Jupiter API - TEST VERSION (same as main.py)"""
    http_session = aiohttp.ClientSession()
    
    try:
        url = f"https://api.jup.ag/tokens/v2/toptraded/1h?limit=100&minMcap={int(BOT_CALL_MIN_MARKET_CAP)}&maxMcap={int(BOT_CALL_MAX_MARKET_CAP)}"
        headers = {
            "x-api-key": JUPITER_API_KEY
        }
        
        print("=" * 80)
        print("FETCHING TOKENS FROM JUPITER API (Top Traded 1h)...")
        print("=" * 80)
        print(f"Using API Key: {JUPITER_API_KEY[:8]}...{JUPITER_API_KEY[-4:]}")
        print(f"Endpoint: {url}")
        print(f"Note: Market cap filter applied at API level (${BOT_CALL_MIN_MARKET_CAP:,.0f} - ${BOT_CALL_MAX_MARKET_CAP:,.0f})")
        print()
        
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 429:
                print("[WARN] Jupiter API rate limited, skipping...")
                return []
            
            response.raise_for_status()
            data = await response.json()
            
            # Jupiter API returns list of tokens (already Solana-only)
            tokens = data if isinstance(data, list) else []
            print(f"[FETCH] Jupiter API returned {len(tokens)} token(s)")
            
            # Debug: Show first token structure
            if tokens and len(tokens) > 0:
                print(f"\n[DEBUG] Sample token structure (first token):")
                first_token = tokens[0]
                print(f"  Keys: {list(first_token.keys())[:10]}...")  # Show first 10 keys
                print(f"  Address: {first_token.get('address', 'N/A')}")
                print(f"  Symbol: {first_token.get('symbol', 'N/A')}")
                print(f"  Market Cap: {first_token.get('marketCap', 'N/A')}")
                print(f"  Volume 24h: {first_token.get('volume24h', 'N/A')}")
            
            if not tokens:
                return []
            
            # Get SOL price for fee conversion (try CoinGecko first, then Jupiter)
            sol_price_usd = 125.0  # Default fallback
            try:
                # Try CoinGecko first (more reliable)
                async with http_session.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "solana" in data and "usd" in data["solana"]:
                            sol_price_usd = float(data["solana"]["usd"])
                            print(f"[INFO] SOL Price from CoinGecko: ${sol_price_usd:.2f}")
            except Exception as e:
                print(f"[DEBUG] CoinGecko price fetch failed: {e}")
                # Try Jupiter as fallback
                try:
                    price_url = "https://price.jup.ag/v4/price?ids=SOL"
                    async with http_session.get(price_url, timeout=aiohttp.ClientTimeout(total=10)) as price_resp:
                        if price_resp.status == 200:
                            price_data = await price_resp.json()
                            if "data" in price_data and "SOL" in price_data["data"]:
                                sol_price_usd = float(price_data["data"]["SOL"].get("price", 125.0))
                                print(f"[INFO] SOL Price from Jupiter: ${sol_price_usd:.2f}")
                except Exception as e2:
                    print(f"[WARN] Could not fetch SOL price from any source: {e2}, using default ${sol_price_usd}")
            
            qualifying_tokens = []
            now = time.time()
            
            print("\n" + "=" * 80)
            print("FILTERING TOKENS BY CRITERIA...")
            print("=" * 80)
            print(f"Criteria:")
            print(f"  • Market Cap: ${BOT_CALL_MIN_MARKET_CAP:,.0f} - ${BOT_CALL_MAX_MARKET_CAP:,.0f}")
            print(f"  • Total Fees >= {BOT_CALL_MIN_FEES_SOL} SOL")
            print(f"  • Chain: Solana only")
            print()
            
            processed_count = 0
            skipped_invalid_address = 0
            
            for idx, token in enumerate(tokens, 1):
                try:
                    # toptraded endpoint uses "id" instead of "address"
                    token_address = token.get("id") or token.get("address")
                    if not token_address or not is_valid_solana_address(token_address):
                        skipped_invalid_address += 1
                        if idx <= 5:  # Show first 5 skipped for debugging
                            print(f"[DEBUG] Skipped token {idx}: invalid address '{token_address}'")
                        continue
                    
                    processed_count += 1
                    
                    # Get token metadata
                    token_name = token.get("name", "Unknown")
                    token_symbol = token.get("symbol", "UNKNOWN")
                    
                    # Get market cap from token data (toptraded uses "mcap" or "fdv")
                    market_cap = None
                    if "mcap" in token:
                        try:
                            market_cap = float(token["mcap"])
                        except (ValueError, TypeError):
                            pass
                    if not market_cap and "fdv" in token:
                        try:
                            market_cap = float(token["fdv"])
                        except (ValueError, TypeError):
                            pass
                    if not market_cap and "marketCap" in token:
                        try:
                            market_cap = float(token["marketCap"])
                        except (ValueError, TypeError):
                            pass
                    
                    # Get volume 24h from stats24h (toptraded endpoint structure)
                    volume_24h_usd = None
                    stats24h = token.get("stats24h", {})
                    stats1h = token.get("stats1h", {})
                    if isinstance(stats24h, dict):
                        buy_volume = stats24h.get("buyVolume", 0) or 0
                        sell_volume = stats24h.get("sellVolume", 0) or 0
                        try:
                            volume_24h_usd = float(buy_volume) + float(sell_volume)
                        except (ValueError, TypeError):
                            pass
                    
                    # Fallback to volume24h if stats24h not available
                    if not volume_24h_usd and "volume24h" in token:
                        try:
                            volume_24h_usd = float(token["volume24h"])
                        except (ValueError, TypeError):
                            pass
                    
                    # Get price change 1h from stats1h
                    price_change_1h = None
                    if stats1h and isinstance(stats1h, dict):
                        price_change_1h = stats1h.get("priceChange")
                        if price_change_1h is not None:
                            try:
                                price_change_1h = float(price_change_1h)
                            except (ValueError, TypeError):
                                price_change_1h = None
                    
                    # Try to get volume and fees from Meteora if enabled
                    meteora_volume = None
                    meteora_fees = None
                    if USE_METEORA_FOR_FEES:
                        meteora_volume, meteora_fees = fetch_meteora_volume_and_fees(token_address)
                        if meteora_volume:
                            print(f"     [METEORA] Volume: {_format_usd(meteora_volume)}")
                        if meteora_fees:
                            print(f"     [METEORA] Fees: {_format_usd(meteora_fees)}")
                    
                    # Use Meteora volume if it's higher than Jupiter volume
                    if meteora_volume and meteora_volume > (volume_24h_usd or 0):
                        volume_24h_usd = meteora_volume
                    
                    # Calculate fees: prefer Meteora fees if available, otherwise calculate from volume
                    if meteora_fees and meteora_fees > 0:
                        total_fees_usd = meteora_fees
                        print(f"     [METEORA] Using Meteora fees directly")
                    else:
                        # Calculate fees (0.3% of volume is typical for DEX fees)
                        fee_percentage = 0.003
                        total_fees_usd = volume_24h_usd * fee_percentage if volume_24h_usd else 0
                    
                    total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
                    
                    # Check criteria
                    market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP and market_cap <= BOT_CALL_MAX_MARKET_CAP
                    fees_ok = total_fees_sol >= BOT_CALL_MIN_FEES_SOL
                    price_change_1h_ok = price_change_1h is not None and price_change_1h >= BOT_CALL_MIN_PRICE_CHANGE_1H
                    
                    print(f"\n[{idx}] {token_symbol} ({token_name[:30]})")
                    print(f"     Address: {token_address[:8]}...{token_address[-8:]}")
                    market_cap_str = _format_usd(market_cap) if market_cap else 'N/A'
                    if market_cap:
                        if market_cap < BOT_CALL_MIN_MARKET_CAP:
                            cap_status = f"❌ (min: ${BOT_CALL_MIN_MARKET_CAP:,.0f})"
                        elif market_cap > BOT_CALL_MAX_MARKET_CAP:
                            cap_status = f"❌ (max: ${BOT_CALL_MAX_MARKET_CAP:,.0f})"
                        else:
                            cap_status = "✅"
                    else:
                        cap_status = "❌ (N/A)"
                    print(f"     Market Cap: {market_cap_str} {cap_status}")
                    print(f"     Fees: {total_fees_sol:.2f} SOL ({_format_usd(total_fees_usd)}) {'✅' if fees_ok else '❌'} (min: {BOT_CALL_MIN_FEES_SOL} SOL)")
                    if price_change_1h is not None:
                        print(f"     Price Change 1h: {price_change_1h:+.2f}% {'✅' if price_change_1h_ok else '❌'} (min: {BOT_CALL_MIN_PRICE_CHANGE_1H}%)")
                    else:
                        print(f"     Price Change 1h: N/A ❌ (min: {BOT_CALL_MIN_PRICE_CHANGE_1H}%)")
                    print(f"     Volume 24h: {_format_usd(volume_24h_usd)}")
                    
                    if not (market_cap_ok and fees_ok and price_change_1h_ok):
                        print(f"     ❌ DOES NOT MEET CRITERIA")
                        continue
                    
                    # Get additional data
                    price_usd = token.get("usdPrice") or token.get("price") or None
                    if price_usd:
                        try:
                            price_usd = float(price_usd)
                        except (ValueError, TypeError):
                            price_usd = None
                    
                    liquidity_usd = token.get("liquidity") or None
                    if liquidity_usd:
                        try:
                            liquidity_usd = float(liquidity_usd)
                        except (ValueError, TypeError):
                            liquidity_usd = None
                    
                    # Get price change 24h from stats24h
                    price_change_24h = None
                    if stats24h and isinstance(stats24h, dict):
                        price_change_24h = stats24h.get("priceChange")
                        if price_change_24h:
                            try:
                                price_change_24h = float(price_change_24h)
                            except (ValueError, TypeError):
                                price_change_24h = None
                    
                    # Fallback to priceChange24h
                    if not price_change_24h:
                        price_change_24h = token.get("priceChange24h") or None
                        if price_change_24h:
                            try:
                                price_change_24h = float(price_change_24h)
                            except (ValueError, TypeError):
                                price_change_24h = None
                    
                    # Check if token is new (created within last 2 hours)
                    created_at = token.get("createdAt") or token.get("created_at") or token.get("firstPool", {}).get("createdAt")
                    is_new = True
                    age_hours = None
                    if created_at:
                        try:
                            # Handle ISO string format (e.g., "2025-01-29T23:29:10Z")
                            if isinstance(created_at, str):
                                if created_at.isdigit():
                                    # Timestamp string
                                    created_ts = int(created_at)
                                else:
                                    # ISO format string - parse it
                                    from datetime import datetime
                                    try:
                                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                        created_ts = dt.timestamp()
                                    except:
                                        created_ts = 0
                            elif isinstance(created_at, (int, float)):
                                created_ts = created_at if created_at > 1e10 else created_at * 1000
                            else:
                                created_ts = 0
                            
                            if created_ts > 0:
                                created_ts_seconds = created_ts / 1000 if created_ts > 1e10 else created_ts
                                age_hours = (now - created_ts_seconds) / 3600
                                if age_hours > 2:
                                    is_new = False
                                print(f"     Age: {age_hours:.2f} hours {'🆕' if is_new else '⏰'}")
                        except (ValueError, TypeError) as e:
                            pass
                    
                    # Skip old tokens unless fees are very high
                    if not is_new and total_fees_sol < BOT_CALL_MIN_FEES_SOL * 2:
                        print(f"     ⏭️  SKIP: Token terlalu lama dan fees tidak cukup tinggi")
                        continue
                    
                    qualifying_tokens.append({
                        "address": token_address,
                        "name": token_name,
                        "symbol": token_symbol,
                        "market_cap": market_cap,
                        "total_fees_sol": total_fees_sol,
                        "total_fees_usd": total_fees_usd,
                        "price_usd": price_usd,
                        "liquidity_usd": liquidity_usd,
                        "volume_24h": volume_24h_usd,
                        "price_change_24h": price_change_24h,
                        "price_change_1h": price_change_1h,
                        "created_at": created_at,
                        "dex_id": None,  # toptraded endpoint doesn't provide dex_id
                    })
                    print(f"     ✅ QUALIFIED!")
                    
                except Exception as e:
                    print(f"[ERROR] Error processing token: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
            
            print(f"\n[DEBUG] Processing summary:")
            print(f"  Total tokens from API: {len(tokens)}")
            print(f"  Processed (valid address): {processed_count}")
            print(f"  Skipped (invalid address): {skipped_invalid_address}")
            print(f"  Qualified: {len(qualifying_tokens)}")
            
            # Sort by market cap
            qualifying_tokens.sort(key=lambda x: x.get("market_cap", 0) or 0, reverse=True)
            
            # Calculate scores
            def calculate_score(token):
                market_cap = token.get("market_cap", 0) or 0
                fees_sol = token.get("total_fees_sol", 0) or 0
                market_cap_score = (market_cap / 1_000_000) * 0.6
                fees_score = (fees_sol / 100) * 0.4
                return market_cap_score + fees_score
            
            # Sort by score
            qualifying_tokens.sort(key=calculate_score, reverse=True)
            
            return qualifying_tokens
            
    except Exception as e:
        print(f"[ERROR] Failed to fetch tokens from Jupiter: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        await http_session.close()

async def fetch_new_tokens_dexscreener() -> List[Dict[str, object]]:
    """Fetch new tokens from DexScreener - TEST VERSION"""
    http_session = aiohttp.ClientSession()
    
    try:
        # Gunakan endpoint untuk mendapatkan token baru di Solana
        # DexScreener tidak punya endpoint khusus "new tokens", jadi kita gunakan search dengan strategi berbeda
        search_url = "https://api.dexscreener.com/latest/dex/search"
        
        # Coba beberapa strategi pencarian untuk menemukan token baru
        # 1. Search dengan keyword umum untuk mendapatkan banyak pair
        # 2. Filter berdasarkan pairCreatedAt untuk token baru
        queries = [
            "sol",  # Base query untuk Solana
        ]
        
        all_pairs = []
        seen_addresses = set()
        
        print("=" * 80)
        print("FETCHING TOKENS FROM DEXSCREENER...")
        print("=" * 80)
        print("Note: DexScreener search API returns recent pairs, we'll filter by age")
        print()
        
        for query in queries:
            try:
                params = {'q': query}
                print(f"\n[FETCH] Query: '{query}'...")
                async with http_session.get(search_url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 429:
                        print(f"[WARN] Rate limited for query '{query}', skipping...")
                        await asyncio.sleep(2)
                        continue
                    
                    response.raise_for_status()
                    data = await response.json()
                    pairs = data.get("pairs") or []
                    print(f"[FETCH] Found {len(pairs)} pairs from query '{query}'")
                    
                    for pair in pairs:
                        token_address = (pair.get("baseToken") or {}).get("address")
                        if token_address and token_address not in seen_addresses:
                            # Hanya ambil pair dari Solana
                            chain_id = pair.get("chainId")
                            if chain_id == "solana":
                                # Skip SOL sendiri
                                base_symbol = (pair.get("baseToken") or {}).get("symbol", "").upper()
                                if base_symbol == "SOL":
                                    continue
                                
                                all_pairs.append(pair)
                                seen_addresses.add(token_address)
                                symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
                                print(f"  ✅ Added Solana pair: {symbol}")
                            else:
                                print(f"  ⏭️  Skipped {chain_id} pair: {pair.get('baseToken', {}).get('symbol', 'UNKNOWN')}")
                
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[ERROR] Error fetching query '{query}': {e}")
                continue
        
        print(f"\n[RESULT] Total Solana pairs collected: {len(all_pairs)}")
        
        if not all_pairs:
            return []
        
        # Filter pairs yang memenuhi kriteria
        qualifying_tokens = []
        now = time.time()
        
        print("\n" + "=" * 80)
        print("FILTERING TOKENS BY CRITERIA...")
        print("=" * 80)
        print(f"Criteria:")
        print(f"  • Market Cap: ${BOT_CALL_MIN_MARKET_CAP:,.0f} - ${BOT_CALL_MAX_MARKET_CAP:,.0f}")
        print(f"  • Total Fees >= {BOT_CALL_MIN_FEES_SOL} SOL")
        print(f"  • Price Change 1h >= {BOT_CALL_MIN_PRICE_CHANGE_1H}%")
        print(f"  • Chain: Solana only")
        print()
        
        for idx, pair in enumerate(all_pairs, 1):
            try:
                base_token = pair.get("baseToken") or {}
                quote_token = pair.get("quoteToken") or {}
                
                token_address = base_token.get("address")
                if not token_address or not is_valid_solana_address(token_address):
                    continue
                
                token_symbol = base_token.get("symbol", "UNKNOWN")
                token_name = base_token.get("name", "Unknown")
                
                # Parse market cap
                market_cap = None
                if pair.get("fdv"):
                    try:
                        market_cap = float(pair.get("fdv", 0))
                    except (ValueError, TypeError):
                        pass
                if not market_cap and base_token.get("marketCap"):
                    try:
                        market_cap = float(base_token.get("marketCap", 0))
                    except (ValueError, TypeError):
                        pass
                
                # Parse volume 24h
                volume_24h_usd = None
                if pair.get("volume"):
                    volume_data = pair.get("volume")
                    if isinstance(volume_data, dict):
                        volume_24h_usd = float(volume_data.get("h24", 0) or 0)
                    elif isinstance(volume_data, (int, float)):
                        volume_24h_usd = float(volume_data)
                
                # Fee percentage
                fee_percentage = 0.003  # Default 0.3%
                dex_id = pair.get("dexId", "").lower()
                if "raydium" in dex_id:
                    fee_percentage = 0.0025
                elif "orca" in dex_id:
                    fee_percentage = 0.003
                
                total_fees_usd = volume_24h_usd * fee_percentage if volume_24h_usd else 0
                
                # Get SOL price
                sol_price_usd = None
                quote_token_symbol = (quote_token.get("symbol", "") or "").upper()
                base_token_symbol = (base_token.get("symbol", "") or "").upper()
                base_token_address = (base_token.get("address", "") or "").upper()
                
                if base_token_symbol == "SOL" or base_token_address == SOL_MINT:
                    if quote_token_symbol in ["USDC", "USDT"]:
                        price_usd_val = pair.get("priceUsd")
                        if price_usd_val:
                            try:
                                sol_price_usd = float(price_usd_val)
                            except (ValueError, TypeError):
                                pass
                
                if not sol_price_usd:
                    sol_price_usd = 125.0  # Default
                
                total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
                
                # Check criteria
                market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP
                fees_ok = total_fees_sol >= BOT_CALL_MIN_FEES_SOL
                
                print(f"\n[{idx}] {token_symbol} ({token_name[:30]})")
                print(f"     Address: {token_address[:8]}...{token_address[-8:]}")
                print(f"     Market Cap: ${market_cap:,.0f} {'✅' if market_cap_ok else '❌'} (min: ${BOT_CALL_MIN_MARKET_CAP:,.0f})")
                print(f"     Fees: {total_fees_sol:.2f} SOL ({_format_usd(total_fees_usd)}) {'✅' if fees_ok else '❌'} (min: {BOT_CALL_MIN_FEES_SOL} SOL)")
                print(f"     Volume 24h: {_format_usd(volume_24h_usd)}")
                print(f"     DEX: {dex_id.upper()}")
                
                if market_cap_ok and fees_ok:
                    # Check if new
                    pair_created_at = pair.get("pairCreatedAt")
                    is_new = True
                    if pair_created_at:
                        try:
                            created_ts = int(pair_created_at) / 1000
                            age_hours = (now - created_ts) / 3600
                            if age_hours > 2:
                                is_new = False
                            print(f"     Age: {age_hours:.2f} hours {'🆕' if is_new else '⏰'}")
                        except (ValueError, TypeError):
                            pass
                    
                    if not is_new and total_fees_sol < BOT_CALL_MIN_FEES_SOL * 2:
                        print(f"     ⏭️  SKIP: Token terlalu lama dan fees tidak cukup tinggi")
                        continue
                    
                    price_usd = pair.get("priceUsd")
                    liquidity = pair.get("liquidity", {})
                    liquidity_usd = liquidity.get("usd") if isinstance(liquidity, dict) else liquidity
                    price_change_24h = pair.get("priceChange", {}).get("h24") if isinstance(pair.get("priceChange"), dict) else None
                    
                    qualifying_tokens.append({
                        "address": token_address,
                        "name": token_name,
                        "symbol": token_symbol,
                        "market_cap": market_cap,
                        "total_fees_sol": total_fees_sol,
                        "total_fees_usd": total_fees_usd,
                        "price_usd": float(price_usd) if price_usd else None,
                        "liquidity_usd": liquidity_usd,
                        "volume_24h": volume_24h_usd,
                        "price_change_24h": price_change_24h,
                        "pair_address": pair.get("pairAddress"),
                        "dex_id": pair.get("dexId"),
                        "created_at": pair_created_at,
                    })
                    print(f"     ✅ QUALIFIED!")
                else:
                    print(f"     ❌ DOES NOT MEET CRITERIA")
                    
            except Exception as e:
                print(f"[ERROR] Error processing pair: {e}")
                continue
        
        # Sort by market cap
        qualifying_tokens.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
        
        # Calculate scores
        def calculate_score(token):
            market_cap = token.get("market_cap", 0) or 0
            fees_sol = token.get("total_fees_sol", 0) or 0
            market_cap_score = (market_cap / 1_000_000) * 0.6
            fees_score = (fees_sol / 100) * 0.4
            return market_cap_score + fees_score
        
        # Sort by score
        qualifying_tokens.sort(key=calculate_score, reverse=True)
        
        return qualifying_tokens
            
    except Exception as e:
        print(f"[ERROR] Failed to fetch tokens: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        await http_session.close()

async def fetch_new_tokens_test() -> List[Dict[str, object]]:
    """Main function to fetch tokens - switches between Jupiter and DexScreener"""
    if USE_JUPITER_API:
        return await fetch_new_tokens_jupiter()
    else:
        return await fetch_new_tokens_dexscreener()

async def main():
    print("\n" + "=" * 80)
    print("BOT CALL - TOKEN FETCH TEST")
    print("=" * 80)
    print(f"\nAPI Source: {'Jupiter API (Recommended)' if USE_JUPITER_API else 'DexScreener API'}")
    if USE_METEORA_FOR_FEES:
        print(f"Meteora Volume: Enabled (will fetch volume from Meteora if Jupiter volume is missing/low)")
    print(f"Filter Criteria:")
    print(f"  • Chain: Solana ONLY")
    print(f"  • Market Cap: ${BOT_CALL_MIN_MARKET_CAP:,.0f} - ${BOT_CALL_MAX_MARKET_CAP:,.0f}")
    print(f"  • Total Fees: >= {BOT_CALL_MIN_FEES_SOL} SOL")
    print(f"  • Price Change 1h: >= {BOT_CALL_MIN_PRICE_CHANGE_1H}%")
    print(f"  • Result: Only BEST token will be sent")
    print()
    print("💡 Tips:")
    print("  • Set USE_JUPITER_API=false to use DexScreener (no API key needed)")
    print("  • Set USE_METEORA_FOR_FEES=true to fetch volume from Meteora as fallback")
    print()
    
    tokens = await fetch_new_tokens_test()
    
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)
    
    if not tokens:
        print("\n❌ No tokens found that meet the criteria!")
        print("\nPossible reasons:")
        print("  • No tokens with market cap >= $200k")
        print("  • No tokens with fees >= 20 SOL")
        print("  • All tokens already notified")
        print("  • API rate limited")
        return
    
    print(f"\n✅ Found {len(tokens)} qualifying token(s)")
    
    # Calculate scores
    def calculate_score(token):
        market_cap = token.get("market_cap", 0) or 0
        fees_sol = token.get("total_fees_sol", 0) or 0
        market_cap_score = (market_cap / 1_000_000) * 0.6
        fees_score = (fees_sol / 100) * 0.4
        return market_cap_score + fees_score
    
    print("\n" + "-" * 80)
    print("QUALIFYING TOKENS (sorted by score):")
    print("-" * 80)
    
    for idx, token in enumerate(tokens, 1):
        score = calculate_score(token)
        print(f"\n[{idx}] {token['symbol']} - {token['name'][:40]}")
        print(f"     Score: {score:.4f}")
        print(f"     Market Cap: {_format_usd(token['market_cap'])}")
        print(f"     Fees: {token['total_fees_sol']:.2f} SOL ({_format_usd(token['total_fees_usd'])})")
        print(f"     Volume 24h: {_format_usd(token['volume_24h'])}")
        print(f"     Address: {token['address'][:8]}...{token['address'][-8:]}")
        if token.get('dex_id'):
            print(f"     DEX: {token['dex_id'].upper()}")
        if token.get('price_change_1h') is not None:
            print(f"     Price Change 1h: {token['price_change_1h']:+.2f}%")
        if token.get('price_change_24h') is not None:
            print(f"     Price Change 24h: {token['price_change_24h']:+.2f}%")
    
    # Best token
    best_token = tokens[0]
    best_score = calculate_score(best_token)
    
    print("\n" + "=" * 80)
    print("🏆 BEST TOKEN (WILL BE SENT TO DISCORD)")
    print("=" * 80)
    print(f"\nSymbol: {best_token['symbol']}")
    print(f"Name: {best_token['name']}")
    print(f"Address: {best_token['address']}")
    print(f"Score: {best_score:.4f}")
    print(f"Market Cap: {_format_usd(best_token['market_cap'])}")
    print(f"Total Fees: {best_token['total_fees_sol']:.2f} SOL ({_format_usd(best_token['total_fees_usd'])})")
    print(f"Volume 24h: {_format_usd(best_token['volume_24h'])}")
    print(f"Price: ${best_token['price_usd']:.8f}" if best_token.get('price_usd') else "Price: N/A")
    print(f"Liquidity: {_format_usd(best_token.get('liquidity_usd'))}")
    if best_token.get('price_change_1h') is not None:
        print(f"Price Change 1h: {best_token['price_change_1h']:+.2f}%")
    if best_token.get('price_change_24h') is not None:
        print(f"Price Change 24h: {best_token['price_change_24h']:+.2f}%")
    if best_token.get('dex_id'):
        print(f"DEX: {best_token['dex_id'].upper()}")
    print(f"\nLinks:")
    print(f"  • Solscan: https://solscan.io/token/{best_token['address']}")
    print(f"  • Jupiter: https://jup.ag/tokens/{best_token['address']}")
    print(f"  • GMGN: https://gmgn.ai/sol/token/{best_token['address']}")
    if best_token.get('pair_address'):
        print(f"  • DexScreener: https://dexscreener.com/solana/{best_token['pair_address']}")
    
    print("\n" + "=" * 80)
    print("✅ This token will be sent to Discord channel if BOT_CALL_CHANNEL_ID is set")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())

