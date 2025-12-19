#!/usr/bin/env python3
"""
Script test untuk melihat contoh bagaimana bot memproses data dari DexScreener
dan menentukan apakah token memenuhi kriteria untuk dikirim notifikasi
"""

import json
import time
from datetime import datetime

# Simulasi data dari DexScreener API (contoh real)
EXAMPLE_PAIR_DATA = {
    "chainId": "solana",
    "dexId": "raydium",
    "url": "https://dexscreener.com/solana/example123",
    "pairAddress": "ExamplePairAddress123",
    "baseToken": {
        "address": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
        "name": "Example Token",
        "symbol": "EXMPL",
        "marketCap": 250000  # 250k USD - MELEBIHI MINIMUM 200k
    },
    "quoteToken": {
        "address": "So11111111111111111111111111111111111111112",
        "name": "Wrapped SOL",
        "symbol": "SOL"
    },
    "priceUsd": "0.0005",
    "priceNative": "0.000004",
    "volume": {
        "h24": 80000,  # Volume 24h: 80k USD
        "h6": 15000,
        "h1": 2000,
        "m5": 500
    },
    "liquidity": {
        "usd": 50000,
        "base": 100000000,
        "quote": 400
    },
    "fdv": 250000,  # Fully Diluted Valuation = Market Cap
    "marketCap": 250000,
    "priceChange": {
        "h24": 15.5,  # +15.5% dalam 24 jam
        "h6": 5.2,
        "h1": 1.1,
        "m5": 0.3
    },
    "pairCreatedAt": int(time.time() * 1000) - (3600 * 1000),  # Dibuat 1 jam yang lalu
}

# Kriteria filter
BOT_CALL_MIN_MARKET_CAP = 200000  # 200k USD
BOT_CALL_MIN_FEES = 20  # 20 USD

def process_pair(pair):
    """Simulasi proses yang dilakukan bot untuk setiap pair"""
    print("=" * 80)
    print("PROSES EVALUASI TOKEN")
    print("=" * 80)
    
    base_token = pair.get("baseToken", {})
    quote_token = pair.get("quoteToken", {})
    
    # 1. Cek chain ID
    chain_id = pair.get("chainId")
    print(f"\n1. Chain ID: {chain_id}")
    if chain_id != "solana":
        print("   ❌ Bukan token Solana, SKIP")
        return None
    print("   ✅ Token Solana")
    
    # 2. Ambil token address
    token_address = base_token.get("address")
    print(f"\n2. Token Address: {token_address}")
    
    # 3. Parse market cap
    market_cap = None
    if pair.get("fdv"):
        market_cap = float(pair.get("fdv", 0))
        print(f"   Market Cap (dari FDV): ${market_cap:,.2f}")
    if not market_cap and base_token.get("marketCap"):
        market_cap = float(base_token.get("marketCap", 0))
        print(f"   Market Cap (dari baseToken): ${market_cap:,.2f}")
    
    # 4. Parse volume 24h
    volume_24h = None
    if pair.get("volume"):
        volume_data = pair.get("volume")
        if isinstance(volume_data, dict):
            volume_24h = float(volume_data.get("h24", 0) or 0)
        elif isinstance(volume_data, (int, float)):
            volume_24h = float(volume_data)
    print(f"\n3. Volume 24h: ${volume_24h:,.2f}" if volume_24h else "\n3. Volume 24h: N/A")
    
    # 5. Hitung total fees
    fee_percentage = 0.003  # 0.3% default
    dex_id = pair.get("dexId", "").lower()
    if "raydium" in dex_id:
        fee_percentage = 0.0025  # 0.25%
    elif "orca" in dex_id:
        fee_percentage = 0.003  # 0.3%
    
    total_fees = volume_24h * fee_percentage if volume_24h else 0
    print(f"4. Fee Percentage: {fee_percentage * 100}% ({dex_id})")
    print(f"   Total Fees 24h: ${total_fees:,.2f}")
    
    # 6. Cek kriteria
    print(f"\n5. EVALUASI KRITERIA:")
    print(f"   Market Cap: ${market_cap:,.2f} >= ${BOT_CALL_MIN_MARKET_CAP:,.2f}? ", end="")
    market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP
    print("✅" if market_cap_ok else "❌")
    
    print(f"   Total Fees: ${total_fees:,.2f} >= ${BOT_CALL_MIN_FEES:,.2f}? ", end="")
    fees_ok = total_fees >= BOT_CALL_MIN_FEES
    print("✅" if fees_ok else "❌")
    
    if not (market_cap_ok and fees_ok):
        print("\n   ❌ Token TIDAK memenuhi kriteria, SKIP")
        return None
    
    # 7. Cek apakah token baru
    pair_created_at = pair.get("pairCreatedAt")
    is_new = True
    if pair_created_at:
        created_ts = int(pair_created_at) / 1000
        age_hours = (time.time() - created_ts) / 3600
        print(f"\n6. Umur Token: {age_hours:.2f} jam")
        if age_hours > 2:
            is_new = False
            print("   ⚠️ Token sudah lebih dari 2 jam, tapi tetap dipertimbangkan jika fees tinggi")
    
    # 8. Prepare data untuk notifikasi
    token_name = base_token.get("name") or "Unknown"
    token_symbol = base_token.get("symbol") or "UNKNOWN"
    price_usd = pair.get("priceUsd")
    liquidity = pair.get("liquidity", {})
    liquidity_usd = liquidity.get("usd") if isinstance(liquidity, dict) else liquidity
    price_change_24h = pair.get("priceChange", {}).get("h24") if isinstance(pair.get("priceChange"), dict) else None
    
    result = {
        "address": token_address,
        "name": token_name,
        "symbol": token_symbol,
        "market_cap": market_cap,
        "total_fees": total_fees,
        "price_usd": float(price_usd) if price_usd else None,
        "liquidity_usd": liquidity_usd,
        "volume_24h": volume_24h,
        "price_change_24h": price_change_24h,
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
        "created_at": pair_created_at,
    }
    
    print("\n" + "=" * 80)
    print("✅ TOKEN MEMENUHI KRITERIA - AKAN DIKIRIM NOTIFIKASI")
    print("=" * 80)
    print("\nData yang akan dikirim:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    return result

def simulate_notification_embed(token_data):
    """Simulasi embed yang akan dikirim ke Discord"""
    print("\n" + "=" * 80)
    print("CONTOH EMBED NOTIFIKASI YANG AKAN DIKIRIM KE DISCORD")
    print("=" * 80)
    
    embed_text = f"""
╔══════════════════════════════════════════════════════════════╗
║  🆕 New Token Detected: {token_data['symbol']:<10}                    ║
╚══════════════════════════════════════════════════════════════╝

📛 Name: {token_data['name']}
🔖 Symbol: {token_data['symbol']}
📍 Address: `{token_data['address'][:8]}...{token_data['address'][-8:]}`

💰 Price: ${token_data['price_usd']:.8f}
📊 Market Cap: ${token_data['market_cap']:,.0f}
💧 Liquidity: ${token_data['liquidity_usd']:,.0f}
📈 Volume (24h): ${token_data['volume_24h']:,.0f}
💵 Total Fees (24h): ${token_data['total_fees']:,.2f}
📉 Price Change (24h): {token_data['price_change_24h']:+.2f}% if token_data['price_change_24h'] else 'N/A'
🏦 DEX: {token_data['dex_id'].upper()}

🔗 Links:
   • Solscan: https://solscan.io/token/{token_data['address']}
   • Jupiter: https://jup.ag/tokens/{token_data['address']}
   • GMGN: https://gmgn.ai/sol/token/{token_data['address']}
   • DexScreener: https://dexscreener.com/solana/{token_data['pair_address']}

═══════════════════════════════════════════════════════════════
"""
    print(embed_text)

if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("SIMULASI BOT CALL LOGIC")
    print("=" * 80)
    print("\nKriteria Filter:")
    print(f"  • Market Cap Minimum: ${BOT_CALL_MIN_MARKET_CAP:,.0f}")
    print(f"  • Total Fees Minimum: ${BOT_CALL_MIN_FEES:,.0f}")
    print(f"  • Chain: Solana only")
    print(f"  • Token Age: Prefer < 2 hours")
    
    # Proses contoh data
    result = process_pair(EXAMPLE_PAIR_DATA)
    
    if result:
        simulate_notification_embed(result)
    
    print("\n" + "=" * 80)
    print("CATATAN:")
    print("=" * 80)
    print("1. Bot akan polling setiap 2 menit (default)")
    print("2. Token yang sudah di-notifikasi akan disimpan di bot_call_state.json")
    print("3. Token yang sama tidak akan di-notifikasi lagi (prevent spam)")
    print("4. State akan di-cleanup setiap 24 jam untuk token lama")
    print("=" * 80)

