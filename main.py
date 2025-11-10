import discord
from discord.ext import commands
import requests
import os
import re
import sys
import json
import time

# --- TOKEN ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
print(f"[DEBUG] Loaded TOKEN? {'✅ Yes' if TOKEN else '❌ No'}")

if not TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

# --- DISCORD INTENTS ---
intents = discord.Intents.default()
intents.message_content = True  # PENTING: untuk baca message content
intents.guilds = True
intents.members = True  # penting untuk event on_member_join
print("[DEBUG] Discord intents sudah diaktifkan")

bot = commands.Bot(command_prefix='!', intents=intents)

# --- GANTI DENGAN CHANNEL & ROLE ID KAMU ---
ALLOWED_CHANNEL_ID = 1428299549507584080  # Channel LP Calls
MENTION_ROLE_ID = 1437345814245801994  # Role yg mau di-mention di thread
AUTO_ROLE_ID = 1437345814245801994  # 🟢 Role default untuk member baru
WELCOME_CHANNEL_ID = 1425708221175173122  # ID channel welcome

# --- EVENT: BOT ONLINE ---
@bot.event
async def on_ready():
    print(f"✅ {bot.user} sudah online dan siap digunakan!")
    print(f"[DEBUG] Connected to {len(bot.guilds)} guild(s): {[g.name for g in bot.guilds]}")

# --- EVENT: MEMBER BARU JOIN ---
@bot.event
async def on_member_join(member: discord.Member):
    print(f"[DEBUG] New member joined: {member.name}")

    # Tambahkan role otomatis
    role = member.guild.get_role(AUTO_ROLE_ID)
    if role:
        try:
            await member.add_roles(role)
            print(f"✅ Role {role.name} diberikan ke {member.name}")
        except discord.Forbidden:
            print(f"❌ Bot tidak punya izin untuk menambahkan role")
        except Exception as e:
            print(f"⚠️ Error saat memberi role otomatis: {e}")
    else:
        print(f"⚠️ Role dengan ID {AUTO_ROLE_ID} tidak ditemukan di server {member.guild.name}")

    # Kirim pesan sambutan
    if WELCOME_CHANNEL_ID:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            await channel.send(
                f"👋 Selamat datang {member.mention}! "
                f"Role **{role.name if role else 'Default'}** sudah diberikan otomatis 🎉\n\n"
                "Welcome Lpeeps👋 Selamat datang di metina.id komunitas Liquidity Provider di Indonesia 🇮🇩. "
                "Biar lebih afdol baca #📜｜rules & #👋｜welcome. Lets grow together 🚀"
            )
            print(f"[DEBUG] Welcome message sent to {member.name}")
        else:
            print(f"⚠️ Channel welcome dengan ID {WELCOME_CHANNEL_ID} tidak ditemukan")

# --- HELPER: CEK VALID SOLANA ADDRESS ---
def is_valid_solana_address(addr: str):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', addr))

# --- HELPER: FETCH POOL DATA ---
def fetch_meteora_pools(ca: str):
    print(f"[DEBUG] Fetching Meteora pools for {ca} using all_by_groups API")
    base_url = 'https://dlmm-api.meteora.ag/pair/all_by_groups'
    
    # OPTIMASI: Gunakan search_term untuk filter di server side, jauh lebih cepat!
    # API akan filter pools yang mengandung contract address ini
    target_contract = ca
    
    try:
        start_time = time.time()
        print(f"[DEBUG] Using all_by_groups API: {base_url}")
        print(f"[DEBUG] Search term: {target_contract}")
        sys.stdout.flush()
        
        params = {
            'search_term': target_contract,  # Filter by contract address
            'sort_key': 'tvl',  # Sort by TVL untuk dapat pools teratas
            'order_by': 'desc',  # Descending order (highest TVL first)
            'limit': 50 # Ambil 50 pools teratas (cukup untuk sort & ambil top 10)
        }
        
        print(f"[DEBUG] Making request with search_term...")
        sys.stdout.flush()
        
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # Extract pools from groups structure
        matching_pools = []
        if isinstance(data, dict) and 'groups' in data:
            for group in data.get('groups', []):
                pools = group.get('pairs', [])
                for pool in pools:
                    try:
                        mint_x = pool.get('mint_x', '').lower()
                        mint_y = pool.get('mint_y', '').lower()
                        target_lower = target_contract.lower()
                        
                        # Double check: pool harus match dengan contract address
                        if target_lower in [mint_x, mint_y]:
                            name = pool.get('name', '').strip()
                            if name:
                                clean_name = name.replace(' DLMM', '').replace('DLMM', '').strip()
                                separator = '/' if '/' in clean_name else '-'
                                parts = clean_name.split(separator)
                                if len(parts) >= 2:
                                    pair_name = f"{parts[0].strip()}-{parts[1].strip()}"
                                else:
                                    pair_name = clean_name
                            else:
                                matching_mint = mint_x if target_lower == mint_x else mint_y
                                pair_name = f"{matching_mint[:8]} Pair"

                            liq = float(pool.get('liquidity', 0))
                            liq_str = f"${liq/1000:.1f}K" if liq >= 1000 else f"${liq:.1f}"
                            bin_step = pool.get('bin_step', 0)
                            address = pool.get('address', '')

                            matching_pools.append({
                                'pair': pair_name,
                                'bin': f"{bin_step}/5",
                                'liq': liq_str,
                                'raw_liq': liq,
                                'address': address
                            })
                    except Exception as e:
                        # Skip error pools, continue
                        continue
        
        total_time = time.time() - start_time
        print(f"[DEBUG] ✅ API request completed in {total_time:.2f} seconds!")
        print(f"[DEBUG] ✅ Found {len(matching_pools)} matching pools (already filtered by API)")
        sys.stdout.flush()
        
        return matching_pools
        
    except requests.exceptions.Timeout:
        print("[ERROR] Request timeout - API tidak merespons dalam 30 detik")
        raise Exception("Request timeout - API tidak merespons. Coba lagi nanti.")
    except requests.exceptions.ConnectionError as e:
        print(f"[ERROR] Connection error: {e}")
        raise Exception(f"Connection error: Tidak bisa connect ke API. {str(e)}")
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP error: {e}")
        raise Exception(f"HTTP error: {e}")
    except Exception as e:
        print(f"[ERROR] Unexpected error in fetch_meteora_pools: {e}")
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        raise

# --- AUTO DETECT: USER PASTE CONTRACT ADDRESS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(f"[DEBUG] Message detected in #{message.channel}: {message.content[:40]}")

    content = message.content.strip()
    if is_valid_solana_address(content):
        print(f"[DEBUG] Valid Solana address detected: {content}")
        try:
            await message.channel.send(f"🔍 Cek pool DLMM untuk token: `{content[:8]}...`")
        except Exception as e:
            print(f"[ERROR] Gagal kirim initial message: {e}")
            return

        try:
            print(f"[DEBUG] Starting to fetch pools for {content}")
            sys.stdout.flush()
            pools = fetch_meteora_pools(content)
            print(f"[DEBUG] Fetch completed, found {len(pools)} pools")
            sys.stdout.flush()
            
            if not pools:
                embed = discord.Embed(
                    title="Pool DLMM Meteora",
                    description=f"Gak ditemuin pool untuk token `{content[:8]}...`",
                    color=0xff0000)
                await message.channel.send(embed=embed)
                return

            print(f"[DEBUG] Sorting pools by liquidity...")
            pools.sort(key=lambda x: x['raw_liq'], reverse=True)
            print(f"[DEBUG] Building embed description...")
            desc = f"Found {len(pools)} pool untuk `{content}`\n\n"

            for i, p in enumerate(pools[:10], 1):
                link = f"https://app.meteora.ag/dlmm/{p['address']}"
                desc += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"

            print(f"[DEBUG] Creating embed object...")
            embed = discord.Embed(title="Meteora Pool Bot", description=desc, color=0x00ff00)
            embed.set_footer(text=f"Requested by {message.author.display_name}")
            print(f"[DEBUG] Sending embed with {len(pools[:10])} pools to channel {message.channel.id}")
            sys.stdout.flush()
            try:
                await message.channel.send(embed=embed)
                print(f"[DEBUG] ✅ Embed sent successfully!")
                sys.stdout.flush()
            except discord.Forbidden:
                print(f"[ERROR] Bot tidak punya permission untuk kirim pesan di channel ini")
                raise
            except discord.HTTPException as e:
                print(f"[ERROR] Discord HTTP error saat kirim embed: {e}")
                raise
        except requests.exceptions.Timeout:
            print("[ERROR] Request timeout")
            await message.channel.send("❌ **Timeout**: API tidak merespons dalam 30 detik. Coba lagi nanti.")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Request error: {e}")
            import traceback
            traceback.print_exc()
            await message.channel.send(f"❌ **Connection Error**: Tidak bisa connect ke API Meteora. Error: {str(e)}")
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            import traceback
            traceback.print_exc()
            await message.channel.send(f"❌ **Error**: {str(e)}")

    # penting supaya command seperti !call tetap bisa jalan
    await bot.process_commands(message)

# --- COMMAND: !call <contract_address> ---
@bot.command(name="call")
@commands.has_any_role("Moderator", "admin")
async def call_token(ctx: commands.Context, ca: str):
    print(f"[DEBUG] !call command triggered by {ctx.author} with ca={ca}")
    lp_calls_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    print(f"[DEBUG] LP Calls Channel found? {'✅' if lp_calls_channel else '❌'}")

    if not lp_calls_channel:
        await ctx.send("❌ Gagal menemukan channel LP Calls. Cek ALLOWED_CHANNEL_ID.")
        return

    if not is_valid_solana_address(ca):
        await ctx.send("⚠️ Invalid Solana address!")
        return

    await ctx.send(f"🔍 Fetching Meteora DLMM pools for `{ca[:8]}...`")

    try:
        print(f"[DEBUG] Starting to fetch pools for !call command")
        pools = fetch_meteora_pools(ca)
        print(f"[DEBUG] Fetch completed, found {len(pools)} pools")
        if not pools:
            await ctx.send(f"Gak ditemuin pool untuk `{ca}`")
            return

        pools.sort(key=lambda x: x['raw_liq'], reverse=True)
        top_pool = pools[0]
        pair_name = top_pool['pair'].replace(" ", "")
        thread_name = f"{pair_name}"

        print(f"[DEBUG] Creating thread: {thread_name}")
        thread = await ctx.channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            reason=f"Thread created by {ctx.author}",
        )

        desc = f"Found {len(pools)} Meteora DLMM pool untuk `{ca}`\n\n"
        for i, p in enumerate(pools[:10], 1):
            link = f"https://app.meteora.ag/dlmm/{p['address']}"
            desc += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"

        embed = discord.Embed(title=f"Meteora DLMM Pools — {pair_name}",
                              description=desc,
                              color=0x00ff00)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")

        mention_text = f"<@&{MENTION_ROLE_ID}>" if MENTION_ROLE_ID else ""

        await thread.send(
            f"{mention_text} 💬 Thread created for `{pair_name}`\n\n"
            f"**Contract Address:** `{ca}`\n"
            f"https://solscan.io/token/{ca}"
        )

        await thread.send(embed=embed)

        thread_link = f"https://discord.com/channels/{ctx.guild.id}/{thread.id}"

        info_embed = discord.Embed(
            title=f"🧵 {pair_name}",
            description=(
                f"**Created by:** {ctx.author.mention}\n"
                f"**Channel:** {ctx.channel.mention}\n"
                f"**Token:** `{ca[:8]}...`\n"
                f"**Top Pool:** {top_pool['pair']} ({top_pool['liq']})\n\n"
                f"[🔗 Open Thread]({thread_link})"),
            color=0x3498db)
        await lp_calls_channel.send(embed=info_embed)
        print("[DEBUG] Thread dan embed berhasil dikirim")

    except requests.exceptions.Timeout:
        print("[ERROR] Request timeout in !call")
        await ctx.send("❌ **Timeout**: API tidak merespons dalam 30 detik. Coba lagi nanti.")
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Request error in !call: {e}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ **Connection Error**: Tidak bisa connect ke API Meteora. Error: {str(e)}")
    except discord.Forbidden:
        print("[ERROR] Bot tidak punya izin untuk buat thread")
        await ctx.send("❌ Bot tidak punya izin untuk buat thread atau kirim pesan di sini.")
    except Exception as e:
        print(f"[ERROR] Unexpected error in !call: {e}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ **Error**: {str(e)}")

# --- RUN BOT ---
print("[DEBUG] Bot starting...")
bot.run(TOKEN)
