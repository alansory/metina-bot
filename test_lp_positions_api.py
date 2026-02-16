"""
Test script untuk verify LP positions menggunakan Meteora API
Test wallet: DkLeB2oaQ185kXqthp4beBMwCkawTb2GzF5nfAApTPLd
"""

import asyncio
import aiohttp
import json

# Test wallet address
TEST_WALLET = "DkLeB2oaQ185kXqthp4beBMwCkawTb2GzF5nfAApTPLd"

async def test_meteora_api():
    """Test Meteora API untuk mencari positions"""
    print("=" * 60)
    print("🧪 Testing Meteora API")
    print("=" * 60)
    print(f"Test Wallet: {TEST_WALLET}")
    print()
    
    async with aiohttp.ClientSession() as session:
        # Try Meteora DLMM API
        url = "https://dlmm-api.meteora.ag/pair/all_by_groups"
        
        print("📡 Querying Meteora DLMM API...")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ API Response received")
                    print(f"   Found {len(data.get('groups', []))} pool groups")
                    
                    # Count total pools
                    total_pools = 0
                    for group in data.get('groups', []):
                        total_pools += len(group.get('pairs', []))
                    
                    print(f"   Total pools: {total_pools}")
                    print()
                    print("💡 Note: Meteora API doesn't directly support wallet position queries")
                    print("   Need to use on-chain queries via RPC")
                    return True
                else:
                    print(f"❌ API Error: {response.status}")
                    text = await response.text()
                    print(f"   {text}")
                    return False
        except Exception as e:
            print(f"❌ Error: {e}")
            return False

async def test_rpc_direct():
    """Test direct RPC call dengan format yang berbeda"""
    print()
    print("=" * 60)
    print("🧪 Testing Direct RPC Query")
    print("=" * 60)
    
    HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=29a076bd-5030-4029-9576-98647d6711bf"
    METEORA_DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
    
    async with aiohttp.ClientSession() as session:
        # Try dengan format yang lebih spesifik
        print(f"Querying program: {METEORA_DLMM_PROGRAM_ID}")
        print(f"Wallet: {TEST_WALLET}")
        print()
        
        # Convert wallet to bytes untuk memcmp
        import base58
        try:
            wallet_bytes = base58.b58decode(TEST_WALLET)
            print(f"✅ Wallet decoded: {len(wallet_bytes)} bytes")
        except Exception as e:
            print(f"❌ Failed to decode wallet: {e}")
            return False
        
        # Try query dengan account type filter
        # Meteora DLMM positions might be a different account type
        # Let's try querying with owner filter using getProgramAccounts
        
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [
                METEORA_DLMM_PROGRAM_ID,
                {
                    "encoding": "jsonParsed",
                    "filters": [
                        {
                            "dataSize": 200  # Try to filter by data size first
                        }
                    ]
                }
            ]
        }
        
        print("📡 Querying with dataSize filter (sample)...")
        try:
            async with session.post(
                HELIUS_RPC_URL,
                json=rpc_payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    
                    if "error" in result:
                        print(f"❌ RPC Error: {result['error']}")
                        return False
                    
                    if "result" in result:
                        accounts = result["result"]
                        print(f"✅ Found {len(accounts)} accounts (sample)")
                        
                        if accounts:
                            # Check first few accounts for owner
                            print("\n📋 Sample accounts:")
                            for i, acc in enumerate(accounts[:3], 1):
                                account_info = acc.get('account', {})
                                owner = account_info.get('owner')
                                print(f"   Account {i}:")
                                print(f"      Address: {acc.get('pubkey', 'N/A')[:16]}...")
                                print(f"      Owner: {owner}")
                                print(f"      Lamports: {account_info.get('lamports', 0)}")
                                
                                # Check if this owner matches our wallet
                                if owner and owner.lower() == TEST_WALLET.lower():
                                    print(f"      ✅ MATCH! This is our wallet's position!")
                            
                            # Now try to find our wallet's positions
                            print(f"\n🔍 Searching for wallet {TEST_WALLET[:16]}...")
                            matching = [acc for acc in accounts if acc.get('account', {}).get('owner', '').lower() == TEST_WALLET.lower()]
                            
                            if matching:
                                print(f"✅ Found {len(matching)} position(s) in sample!")
                                return True
                            else:
                                print(f"⚠️ No positions found in sample of {len(accounts)} accounts")
                                print(f"   (This is just a sample - full query needed)")
                        else:
                            print("⚠️ No accounts returned")
                else:
                    print(f"❌ HTTP Error: {response.status}")
                    return False
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return False

async def main():
    """Main test function"""
    print()
    print("🚀 Starting LP Positions API Test")
    print()
    
    # Test 1: Meteora API
    await test_meteora_api()
    
    # Test 2: Direct RPC
    await test_rpc_direct()
    
    print()
    print("=" * 60)
    print("📝 Summary")
    print("=" * 60)
    print("If no positions found, possible reasons:")
    print("  1. Wallet doesn't have LP positions")
    print("  2. Positions are in a different program")
    print("  3. Need to verify on Solana Explorer:")
    print(f"     https://solscan.io/account/{TEST_WALLET}")
    print("=" * 60)
    print()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Test interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()



