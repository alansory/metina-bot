#!/usr/bin/env python3
"""
Script test untuk mengirim bot call ke Discord channel
"""

import asyncio
import aiohttp
import json
import time
import os
import re
import importlib.util
from datetime import datetime
from typing import Dict, List, Optional

# Import functions from test_bot_call_fetch
# We'll import the module and use its functions
import importlib.util
spec = importlib.util.spec_from_file_location("test_bot_call_fetch", os.path.join(os.path.dirname(__file__), "test_bot_call_fetch.py"))
test_fetch_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test_fetch_module)

fetch_new_tokens_test = test_fetch_module.fetch_new_tokens_test
BOT_CALL_MIN_MARKET_CAP = test_fetch_module.BOT_CALL_MIN_MARKET_CAP
BOT_CALL_MAX_MARKET_CAP = test_fetch_module.BOT_CALL_MAX_MARKET_CAP
BOT_CALL_MIN_FEES_SOL = test_fetch_module.BOT_CALL_MIN_FEES_SOL
_format_usd = test_fetch_module._format_usd

# Discord config
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BOT_CALL_CHANNEL_ID = int(os.getenv("BOT_CALL_CHANNEL_ID", "0")) or None

async def send_test_notification(token_data: Dict[str, object]):
    """Send test notification to Discord channel."""
    if not DISCORD_BOT_TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
        return False
    
    if not BOT_CALL_CHANNEL_ID:
        print("❌ ERROR: BOT_CALL_CHANNEL_ID not set!")
        return False
    
    try:
        import discord
        from discord import Embed
        
        # Create Discord client
        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        
        @client.event
        async def on_ready():
            print(f"✅ Bot logged in as {client.user}")
            
            channel = client.get_channel(BOT_CALL_CHANNEL_ID)
            if not channel:
                print(f"❌ Channel with ID {BOT_CALL_CHANNEL_ID} not found!")
                await client.close()
                return
            
            print(f"✅ Found channel: {channel.name} ({channel.id})")
            
            # Prepare token data
            token_address = token_data.get("address")
            token_name = token_data.get("name", "Unknown")
            token_symbol = token_data.get("symbol", "UNKNOWN")
            market_cap = token_data.get("market_cap")
            total_fees_sol = token_data.get("total_fees_sol", 0)
            total_fees_usd = token_data.get("total_fees_usd", 0)
            price_usd = token_data.get("price_usd")
            liquidity_usd = token_data.get("liquidity_usd")
            volume_24h = token_data.get("volume_24h")
            price_change_24h = token_data.get("price_change_24h")
            
            # Format values
            market_cap_str = _format_usd(market_cap)
            fees_sol_str = f"{total_fees_sol:.2f} SOL" if total_fees_sol > 0 else "N/A"
            fees_usd_str = _format_usd(total_fees_usd) if total_fees_usd > 0 else "N/A"
            
            # Create embed
            embed = Embed(
                title=f"🆕 New Token Detected: {token_symbol}",
                description=f"**{token_name}** (`{token_symbol}`)\n\nToken baru terdeteksi dengan kriteria:\n• Market Cap: {market_cap_str}\n• Total Fees (24h): {fees_sol_str} ({fees_usd_str})",
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(name="Market Cap", value=market_cap_str, inline=True)
            embed.add_field(name="Total Fees (24h)", value=f"{fees_sol_str}\n({fees_usd_str})", inline=True)
            
            if price_usd:
                embed.add_field(name="Price", value=f"${price_usd:.8f}", inline=True)
            
            if liquidity_usd:
                embed.add_field(name="Liquidity", value=_format_usd(liquidity_usd), inline=True)
            
            if volume_24h:
                embed.add_field(name="Volume (24h)", value=_format_usd(volume_24h), inline=True)
            
            if price_change_24h is not None:
                change_emoji = "📈" if price_change_24h >= 0 else "📉"
                embed.add_field(name="Price Change (24h)", value=f"{change_emoji} {price_change_24h:+.2f}%", inline=True)
            
            # Add links
            links_value = (
                f"[🔍 Solscan](https://solscan.io/token/{token_address})\n"
                f"[🪐 Jupiter](https://jup.ag/tokens/{token_address})\n"
                f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})"
            )
            embed.add_field(name="🔗 Links", value=links_value, inline=False)
            
            embed.set_footer(text=f"Token Address: {token_address[:8]}...{token_address[-8:]}")
            
            # Send message
            try:
                await channel.send(embed=embed)
                print(f"✅ Successfully sent notification for {token_symbol} ({token_address[:8]}...)")
                print(f"   Channel: {channel.name} ({channel.id})")
            except Exception as e:
                print(f"❌ Failed to send message: {e}")
            
            await client.close()
        
        # Run client
        await client.start(DISCORD_BOT_TOKEN)
        return True
        
    except ImportError:
        print("❌ ERROR: discord.py not installed!")
        print("   Install with: pip install discord.py")
        return False
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

async def main():
    print("\n" + "=" * 80)
    print("BOT CALL - DISCORD SEND TEST")
    print("=" * 80)
    print()
    
    # Check config
    if not DISCORD_BOT_TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN environment variable not set!")
        print("   Set it with: export DISCORD_BOT_TOKEN=your_token")
        return
    
    if not BOT_CALL_CHANNEL_ID:
        print("❌ ERROR: BOT_CALL_CHANNEL_ID environment variable not set!")
        print("   Set it with: export BOT_CALL_CHANNEL_ID=your_channel_id")
        return
    
    print(f"✅ Discord Bot Token: {'*' * 20}...{DISCORD_BOT_TOKEN[-4:]}")
    print(f"✅ Channel ID: {BOT_CALL_CHANNEL_ID}")
    print()
    
    # Fetch tokens
    print("Fetching tokens from API...")
    tokens = await fetch_new_tokens_test()
    
    if not tokens:
        print("\n❌ No tokens found that meet the criteria!")
        print("   Cannot send test notification.")
        return
    
    # Get best token
    def calculate_score(token):
        market_cap = token.get("market_cap", 0) or 0
        fees_sol = token.get("total_fees_sol", 0) or 0
        market_cap_score = (market_cap / 1_000_000) * 0.6
        fees_score = (fees_sol / 100) * 0.4
        return market_cap_score + fees_score
    
    tokens.sort(key=calculate_score, reverse=True)
    best_token = tokens[0]
    
    print(f"\n✅ Found {len(tokens)} qualifying token(s)")
    print(f"🏆 Best token: {best_token['symbol']} - {best_token['name']}")
    print(f"   Market Cap: {_format_usd(best_token['market_cap'])}")
    print(f"   Fees: {best_token['total_fees_sol']:.2f} SOL")
    print()
    
    # Confirm before sending
    print("=" * 80)
    print("⚠️  READY TO SEND TO DISCORD")
    print("=" * 80)
    print(f"Channel ID: {BOT_CALL_CHANNEL_ID}")
    print(f"Token: {best_token['symbol']} ({best_token['name']})")
    print(f"Address: {best_token['address']}")
    print()
    
    # Send notification
    print("Sending notification to Discord...")
    success = await send_test_notification(best_token)
    
    if success:
        print("\n" + "=" * 80)
        print("✅ TEST COMPLETED")
        print("=" * 80)
        print("Check your Discord channel to see the notification!")
    else:
        print("\n" + "=" * 80)
        print("❌ TEST FAILED")
        print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())

