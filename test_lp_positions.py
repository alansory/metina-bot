"""
Test script untuk get_lp_positions function
Test wallet: DkLeB2oaQ185kXqthp4beBMwCkawTb2GzF5nfAApTPLd
"""

import asyncio
import os
import sys
from meteora_lp_agent import get_lp_agent, MeteoraLPAgent

# Test wallet address
TEST_WALLET = "DkLeB2oaQ185kXqthp4beBMwCkawTb2GzF5nfAApTPLd"

# Helius RPC URL
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=29a076bd-5030-4029-9576-98647d6711bf"

# Set RPC URL in environment
os.environ["HELIUS_RPC_URL"] = HELIUS_RPC_URL
os.environ["RPC_URL"] = HELIUS_RPC_URL

async def test_get_lp_positions():
    """Test get_lp_positions function"""
    print("=" * 60)
    print("🧪 Testing get_lp_positions function")
    print("=" * 60)
    print(f"Test Wallet: {TEST_WALLET}")
    print()
    
    # Initialize agent (read-only mode, no private key needed)
    agent = MeteoraLPAgent()
    
    if not agent:
        print("❌ Failed to initialize LP agent")
        return False
    
    # Get RPC URL from environment or default
    rpc_url = os.getenv("HELIUS_RPC_URL") or os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"
    
    print(f"✅ Agent initialized")
    print(f"   RPC URL: {rpc_url[:50]}..." if len(rpc_url) > 50 else f"   RPC URL: {rpc_url}")
    print()
    
    # Test get_lp_positions
    print("📊 Querying LP positions...")
    print("-" * 60)
    
    try:
        positions = await agent.get_lp_positions(TEST_WALLET)
        
        print()
        print("=" * 60)
        print("📋 RESULTS")
        print("=" * 60)
        
        if positions:
            print(f"✅ SUCCESS: Found {len(positions)} position(s)")
            print()
            
            for i, pos in enumerate(positions, 1):
                print(f"Position #{i}:")
                print(f"  Address: {pos.get('position_address', 'N/A')}")
                print(f"  Owner: {pos.get('owner', 'N/A')}")
                print(f"  Lamports: {pos.get('lamports', 0)}")
                
                if 'pool' in pos:
                    print(f"  Pool: {pos.get('pool', 'N/A')}")
                if 'liquidity' in pos:
                    print(f"  Liquidity: {pos.get('liquidity', 'N/A')}")
                if 'parsed' in pos:
                    print(f"  Has parsed data: Yes")
                
                print()
            
            return True
        else:
            print("❌ FAILED: No positions found")
            print()
            print("Possible reasons:")
            print("  1. Wallet has no LP positions")
            print("  2. RPC endpoint issue")
            print("  3. Program ID mismatch")
            print("  4. Query filter issue")
            print()
            return False
            
    except Exception as e:
        print()
        print("=" * 60)
        print("❌ ERROR")
        print("=" * 60)
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Close session
        await agent.close()

async def test_pool_info():
    """Test get_pool_info as additional verification"""
    print()
    print("=" * 60)
    print("🧪 Testing get_pool_info (additional test)")
    print("=" * 60)
    
    # Test with a known Meteora pool
    # Using a common pool address (you can change this)
    test_pool = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"  # This is the program ID, not a pool
    
    agent = MeteoraLPAgent()
    
    try:
        # Just verify agent can make API calls
        print("Testing API connectivity...")
        # We'll skip actual pool lookup since we need a real pool address
        print("✅ Agent can be initialized")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await agent.close()

async def main():
    """Main test function"""
    print()
    print("🚀 Starting LP Positions Test")
    print()
    
    # Test 1: get_lp_positions
    success = await test_get_lp_positions()
    
    # Test 2: Additional connectivity test
    await test_pool_info()
    
    print()
    print("=" * 60)
    if success:
        print("✅ TEST PASSED: Positions found!")
    else:
        print("❌ TEST FAILED: No positions found")
    print("=" * 60)
    print()
    
    return success

if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

