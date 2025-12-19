#!/usr/bin/env python3
"""
Script test untuk melihat contoh hasil API DexScreener
"""

import requests
import json
import time

def test_dexscreener_search():
    """Test DexScreener search API"""
    print("=" * 80)
    print("TEST 1: DexScreener Search API")
    print("=" * 80)
    
    url = "https://api.dexscreener.com/latest/dex/search"
    params = {'q': 'sol'}
    
    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"Status Code: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print("\n" + "-" * 80)
        
        if response.status_code == 200:
            data = response.json()
            print(f"Response Keys: {list(data.keys())}")
            print(f"Number of pairs: {len(data.get('pairs', []))}")
            
            # Ambil 3 pair pertama sebagai contoh
            pairs = data.get('pairs', [])[:3]
            
            for i, pair in enumerate(pairs, 1):
                print(f"\n{'=' * 80}")
                print(f"PAIR #{i}")
                print(f"{'=' * 80}")
                print(json.dumps(pair, indent=2, ensure_ascii=False))
                print(f"\n--- Summary Pair #{i} ---")
                print(f"Chain ID: {pair.get('chainId')}")
                print(f"DEX ID: {pair.get('dexId')}")
                print(f"Pair Address: {pair.get('pairAddress')}")
                print(f"Base Token: {pair.get('baseToken', {}).get('name')} ({pair.get('baseToken', {}).get('symbol')})")
                print(f"Base Token Address: {pair.get('baseToken', {}).get('address')}")
                print(f"Quote Token: {pair.get('quoteToken', {}).get('name')} ({pair.get('quoteToken', {}).get('symbol')})")
                print(f"Price USD: {pair.get('priceUsd')}")
                print(f"FDV: {pair.get('fdv')}")
                print(f"Market Cap: {pair.get('baseToken', {}).get('marketCap')}")
                print(f"Volume 24h: {pair.get('volume', {})}")
                print(f"Liquidity: {pair.get('liquidity', {})}")
                print(f"Pair Created At: {pair.get('pairCreatedAt')}")
                print(f"Price Change 24h: {pair.get('priceChange', {})}")
        else:
            print(f"Error: {response.text}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

def test_dexscreener_latest():
    """Test DexScreener latest tokens API"""
    print("\n\n" + "=" * 80)
    print("TEST 2: DexScreener Latest Tokens API")
    print("=" * 80)
    
    # Contoh dengan beberapa token address Solana yang populer
    test_tokens = [
        "So11111111111111111111111111111111111111112",  # SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    ]
    
    for token_address in test_tokens:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        
        try:
            print(f"\nFetching data for token: {token_address[:8]}...")
            response = requests.get(url, timeout=15)
            print(f"Status Code: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"Response Keys: {list(data.keys())}")
                
                pairs = data.get('pairs', [])
                print(f"Number of pairs: {len(pairs)}")
                
                if pairs:
                    # Ambil pair pertama sebagai contoh
                    pair = pairs[0]
                    print(f"\n{'=' * 80}")
                    print(f"EXAMPLE PAIR FOR TOKEN {token_address[:8]}...")
                    print(f"{'=' * 80}")
                    print(json.dumps(pair, indent=2, ensure_ascii=False))
            else:
                print(f"Error: {response.text}")
            
            time.sleep(1)  # Rate limiting
        except Exception as e:
            print(f"Error: {e}")

def analyze_pair_structure(pair):
    """Analyze structure of a pair and extract relevant fields"""
    print("\n" + "=" * 80)
    print("ANALYZED PAIR STRUCTURE")
    print("=" * 80)
    
    base_token = pair.get('baseToken', {})
    quote_token = pair.get('quoteToken', {})
    volume = pair.get('volume', {})
    liquidity = pair.get('liquidity', {})
    price_change = pair.get('priceChange', {})
    
    analysis = {
        'chain_id': pair.get('chainId'),
        'dex_id': pair.get('dexId'),
        'pair_address': pair.get('pairAddress'),
        'base_token': {
            'address': base_token.get('address'),
            'name': base_token.get('name'),
            'symbol': base_token.get('symbol'),
            'market_cap': base_token.get('marketCap'),
        },
        'quote_token': {
            'address': quote_token.get('address'),
            'name': quote_token.get('name'),
            'symbol': quote_token.get('symbol'),
        },
        'price': {
            'usd': pair.get('priceUsd'),
            'native': pair.get('priceNative'),
        },
        'market_data': {
            'fdv': pair.get('fdv'),
            'market_cap': pair.get('marketCap'),
        },
        'volume_24h': volume.get('h24') if isinstance(volume, dict) else volume,
        'liquidity_usd': liquidity.get('usd') if isinstance(liquidity, dict) else liquidity,
        'price_change_24h': price_change.get('h24') if isinstance(price_change, dict) else price_change,
        'pair_created_at': pair.get('pairCreatedAt'),
    }
    
    print(json.dumps(analysis, indent=2, ensure_ascii=False))
    
    # Calculate fees example
    volume_24h = analysis['volume_24h']
    if volume_24h:
        fee_percentage = 0.003  # 0.3%
        total_fees = float(volume_24h) * fee_percentage
        print(f"\n--- Fee Calculation Example ---")
        print(f"Volume 24h: ${volume_24h:,.2f}")
        print(f"Fee %: {fee_percentage * 100}%")
        print(f"Total Fees 24h: ${total_fees:,.2f}")

if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("DEXSCREENER API TEST SCRIPT")
    print("=" * 80)
    print("\nThis script will test DexScreener API endpoints")
    print("to show example responses for token monitoring.\n")
    
    # Test 1: Search API
    test_dexscreener_search()
    
    # Test 2: Latest tokens API
    test_dexscreener_latest()
    
    print("\n" + "=" * 80)
    print("TEST COMPLETED")
    print("=" * 80)
    print("\nNote: The actual API responses may vary.")
    print("This is just to show the structure of the data.")

