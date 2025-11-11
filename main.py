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
AUTO_ROLE_ID = 1437345814245801994  # 🟢 Role default untuk member baru (setelah verifikasi)
UNVERIFIED_ROLE_ID = 1437655801354522684  # Role unverified untuk member baru
VERIFY_CHANNEL_ID = 1437656297276444682  # Channel verify-here
WELCOME_CHANNEL_ID = 1425708221175173122  # ID channel welcome

# --- FITUR BARU: TRACK WALLET ---
FEATURE_CHANNEL_ID = 1437710602301739053  # Channel untuk setup fitur (ganti dengan ID channel fitur kamu, misalnya #setup-fitur)
TRACK_WALLET_EMOJI = "💼"  # Emoji untuk react track wallet
TRACK_WALLET_ROLE_ID = 1437711623178686546  # Role untuk akses track wallet (buat role baru di server)
TRACK_WALLET_CHANNEL_ID = 1437712394200809482  # Channel private untuk track wallet (set private, hanya visible untuk role ini)

# --- HELPER: SETUP VERIFY MESSAGE ---
async def setup_verify_message():
    """Setup pesan verifikasi di channel verify-here"""
    try:
        verify_channel = bot.get_channel(VERIFY_CHANNEL_ID)
        if not verify_channel:
            print(f"⚠️ Channel verify-here dengan ID {VERIFY_CHANNEL_ID} tidak ditemukan")
            return
        
        # Cek apakah sudah ada pesan verifikasi dari bot
        async for message in verify_channel.history(limit=50):
            if message.author == bot.user and message.embeds:
                # Cek apakah ini pesan verifikasi (biasanya ada embed dengan title tertentu)
                if message.embeds and len(message.embeds) > 0:
                    embed_title = message.embeds[0].title or ""
                    if "verifikasi" in embed_title.lower() or "verify" in embed_title.lower():
                        print(f"[DEBUG] Pesan verifikasi sudah ada di channel verify-here (Message ID: {message.id})")
                        # Pastikan reaction masih ada
                        if not message.reactions:
                            await message.add_reaction("✅")
                            print(f"[DEBUG] Reaction ✅ ditambahkan ke pesan verifikasi yang sudah ada")
                        return
        
        # Buat pesan verifikasi baru
        embed = discord.Embed(
            title="✅ Verifikasi Member",
            description=(
                "**Selamat datang di Metina LP Army!** 🎉\n\n"
                "Untuk mendapatkan akses penuh ke server, silakan klik reaction **✅** di bawah ini untuk verifikasi.\n\n"
                "Setelah verifikasi, kamu akan mendapatkan role member dan bisa mengakses semua channel di server.\n\n"
                "**Cara verifikasi:**\n"
                "1. Klik emoji ✅ di bawah pesan ini\n"
                "2. Tunggu beberapa detik\n"
                "3. Role member akan diberikan otomatis! 🚀"
            ),
            color=0x00ff00
        )
        embed.set_footer(text="Klik reaction ✅ untuk verifikasi")
        
        verify_message = await verify_channel.send(embed=embed)
        await verify_message.add_reaction("✅")
        print(f"✅ Pesan verifikasi berhasil dibuat di channel verify-here (Message ID: {verify_message.id})")
        
    except discord.Forbidden:
        print(f"❌ Bot tidak punya izin untuk kirim pesan atau tambah reaction di channel verify-here (ID: {VERIFY_CHANNEL_ID})")
    except Exception as e:
        print(f"⚠️ Error saat setup pesan verifikasi: {e}")
        import traceback
        traceback.print_exc()

# --- HELPER: SETUP FEATURE MESSAGE ---
async def setup_feature_message():
    """Setup pesan setup fitur di channel fitur (multiple reactions untuk berbagai fitur)"""
    try:
        feature_channel = bot.get_channel(FEATURE_CHANNEL_ID)
        if not feature_channel:
            print(f"⚠️ Channel fitur dengan ID {FEATURE_CHANNEL_ID} tidak ditemukan")
            return
        
        # Cek apakah sudah ada pesan fitur dari bot
        async for message in feature_channel.history(limit=50):
            if message.author == bot.user and message.embeds:
                # Cek apakah ini pesan fitur (title mengandung "fitur")
                if message.embeds and len(message.embeds) > 0:
                    embed_title = message.embeds[0].title or ""
                    if "fitur" in embed_title.lower():
                        print(f"[DEBUG] Pesan fitur sudah ada di channel fitur (Message ID: {message.id})")
                        # Pastikan reactions masih ada
                        reactions_to_add = [TRACK_WALLET_EMOJI]  # Tambah reactions yang diperlukan
                        for emoji in reactions_to_add:
                            if not any(r.emoji == emoji for r in message.reactions):
                                await message.add_reaction(emoji)
                                print(f"[DEBUG] Reaction {emoji} ditambahkan ke pesan fitur yang sudah ada")
                        return
        
        # Buat pesan fitur baru
        embed = discord.Embed(
            title="⚙️ Setup Fitur Tambahan",
            description=(
                "**Pilih fitur yang ingin kamu aktifkan dengan klik reaction di bawah!** 🔧\n\n"
                "**Fitur Tersedia:**\n"
                "• 💼 **Track Wallet**: Aktifkan tracking wallet. Setelah aktif, channel #tracker akan terbuka dan role diberikan.\n\n"
                "(Fitur lain bisa ditambah di sini nanti)\n\n"
                "**Cara setup:**\n"
                "1. Klik emoji fitur yang diinginkan\n"
                "2. Tunggu konfirmasi\n"
                "3. Channel & role akan otomatis diakses! 🚀"
            ),
            color=0x9b59b6
        )
        embed.set_footer(text="Klik reaction untuk aktifkan fitur")
        
        feature_message = await feature_channel.send(embed=embed)
        await feature_message.add_reaction(TRACK_WALLET_EMOJI)
        print(f"✅ Pesan fitur berhasil dibuat di channel fitur (Message ID: {feature_message.id})")
        
    except discord.Forbidden:
        print(f"❌ Bot tidak punya izin untuk kirim pesan atau tambah reaction di channel fitur (ID: {FEATURE_CHANNEL_ID})")
    except Exception as e:
        print(f"⚠️ Error saat setup pesan fitur: {e}")
        import traceback
        traceback.print_exc()

# --- EVENT: BOT ONLINE ---
@bot.event
async def on_ready():
    print(f"✅ {bot.user} sudah online dan siap digunakan!")
    print(f"[DEBUG] Connected to {len(bot.guilds)} guild(s): {[g.name for g in bot.guilds]}")
    
    # Setup pesan verifikasi
    await setup_verify_message()
    
    # Setup pesan fitur (BARU)
    await setup_feature_message()

# --- EVENT: MEMBER BARU JOIN ---
@bot.event
async def on_member_join(member: discord.Member):
    print(f"[DEBUG] New member joined: {member.name}")

    # Tambahkan role unverified otomatis
    unverified_role = member.guild.get_role(UNVERIFIED_ROLE_ID)
    if unverified_role:
        try:
            await member.add_roles(unverified_role)
            print(f"✅ Role {unverified_role.name} diberikan ke {member.name}")
        except discord.Forbidden:
            print("❌ Bot tidak punya izin untuk menambahkan role unverified")
        except Exception as e:
            print(f"⚠️ Error saat memberi role unverified: {e}")
    else:
        print(f"⚠️ Role dengan ID {UNVERIFIED_ROLE_ID} tidak ditemukan di server {member.guild.name}")

    # Kirim pesan sambutan
    if WELCOME_CHANNEL_ID:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            try:
                verify_channel_mention = f"<#{VERIFY_CHANNEL_ID}>" if VERIFY_CHANNEL_ID else "channel verify-here"
                feature_channel_mention = f"<#{FEATURE_CHANNEL_ID}>"
                await channel.send(
                    f"👋 Selamat datang {member.mention}!\n\n"
                    "Welcome Lpeeps👋 Selamat datang di metina.id komunitas Liquidity Provider di Indonesia 🇮🇩. "
                    "Biar lebih afdol baca #📜｜rules & #👋｜welcome. Lets grow together 🚀\n\n"
                    f"⚠️ **Penting:** Silakan verifikasi diri kamu di {verify_channel_mention} untuk mendapatkan akses penuh ke server! ✅\n\n"
                    f"💡 **Fitur Tambahan:** Cek {feature_channel_mention} untuk aktifkan fitur seperti Track Wallet! 💼"
                )
                print(f"[DEBUG] Welcome message sent to {member.name}")
            except discord.Forbidden:
                print(f"❌ Bot tidak punya izin untuk kirim pesan di channel welcome (ID: {WELCOME_CHANNEL_ID})")
            except discord.HTTPException as e:
                print(f"⚠️ HTTP error saat kirim welcome message: {e}")
            except Exception as e:
                print(f"⚠️ Error saat kirim welcome message: {e}")
        else:
            print(f"⚠️ Channel welcome dengan ID {WELCOME_CHANNEL_ID} tidak ditemukan")

# --- HELPER: CEK VALID SOLANA ADDRESS ---
def is_valid_solana_address(addr: str):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', addr))

# --- HELPER: CEK VALID SOLANA WALLET ADDRESS ---
def is_valid_solana_wallet(addr: str):
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
        # Cek apakah ini di channel tracker wallet
        if message.channel.id == TRACK_WALLET_CHANNEL_ID:
            # Handle sebagai wallet tracking
            try:
                embed = discord.Embed(
                    title="💼 Wallet Tracking",
                    description=f"🔍 Mulai tracking wallet: `{content[:8]}...`\n\n**Status:** Aktif! Bot akan monitor transaksi Solana kamu.\n\n*(Fitur tracking detail akan diimplementasikan nanti - sementara ini konfirmasi saja)*",
                    color=0x00ff00
                )
                embed.set_footer(text=f"Wallet: {content}")
                await message.reply(embed=embed)
                print(f"[DEBUG] Wallet tracking started for {message.author}: {content}")
            except Exception as e:
                print(f"[ERROR] Error handling wallet track: {e}")
        else:
            # Handle sebagai token pool check (kode lama)
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

# --- EVENT: REACTION ADD (VERIFICATION & FITUR) ---
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Cek apakah reaction di channel verify-here
    if payload.channel_id == VERIFY_CHANNEL_ID:
        # Handle verifikasi (kode lama)
        if str(payload.emoji) != "✅":
            return
        
        # Cek apakah user bukan bot
        if payload.member and payload.member.bot:
            return
        
        print(f"[DEBUG] Reaction ✅ detected in verify channel by {payload.member.name if payload.member else 'Unknown'}")
        
        try:
            guild = bot.get_guild(payload.guild_id)
            if not guild:
                print(f"⚠️ Guild dengan ID {payload.guild_id} tidak ditemukan")
                return
            
            member = guild.get_member(payload.user_id)
            if not member:
                print(f"⚠️ Member dengan ID {payload.user_id} tidak ditemukan")
                return
            
            # Cek apakah member punya role unverified
            unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
            verified_role = guild.get_role(AUTO_ROLE_ID)
            
            if not unverified_role:
                print(f"⚠️ Role unverified dengan ID {UNVERIFIED_ROLE_ID} tidak ditemukan")
                return
            
            if not verified_role:
                print(f"⚠️ Role verified dengan ID {AUTO_ROLE_ID} tidak ditemukan")
                return
            
            # Cek apakah member punya role unverified
            if unverified_role not in member.roles:
                print(f"[DEBUG] Member {member.name} tidak punya role unverified, skip")
                return
            
            # Hapus role unverified dan tambahkan role verified
            try:
                await member.remove_roles(unverified_role)
                await member.add_roles(verified_role)
                print(f"✅ {member.name} berhasil diverifikasi! Role {unverified_role.name} dihapus, role {verified_role.name} ditambahkan")
                
                # Kirim DM konfirmasi (optional)
                try:
                    await member.send(
                        f"✅ **Verifikasi Berhasil!**\n\n"
                        f"Selamat {member.mention}! Kamu sudah berhasil diverifikasi di **{guild.name}**.\n"
                        f"Role **{verified_role.name}** sudah diberikan. Selamat bergabung! 🎉"
                    )
                except discord.Forbidden:
                    # User mungkin menutup DM, tidak masalah
                    print(f"[DEBUG] Tidak bisa kirim DM ke {member.name} (DM mungkin ditutup)")
            except discord.Forbidden:
                print(f"❌ Bot tidak punya izin untuk mengubah role member {member.name}")
            except Exception as e:
                print(f"⚠️ Error saat memverifikasi member {member.name}: {e}")
                import traceback
                traceback.print_exc()
                
        except Exception as e:
            print(f"[ERROR] Unexpected error in on_raw_reaction_add (verify): {e}")
            import traceback
            traceback.print_exc()
    
    # BARU: Handle reaction di channel fitur
    elif payload.channel_id == FEATURE_CHANNEL_ID:
        if str(payload.emoji) != TRACK_WALLET_EMOJI:
            return  # Hanya handle emoji track wallet untuk sekarang
        
        # Cek apakah user bukan bot
        if payload.member and payload.member.bot:
            return
        
        print(f"[DEBUG] Reaction {TRACK_WALLET_EMOJI} detected in feature channel by {payload.member.name if payload.member else 'Unknown'}")
        
        try:
            guild = bot.get_guild(payload.guild_id)
            if not guild:
                print(f"⚠️ Guild dengan ID {payload.guild_id} tidak ditemukan")
                return
            
            member = guild.get_member(payload.user_id)
            if not member:
                print(f"⚠️ Member dengan ID {payload.user_id} tidak ditemukan")
                return
            
            # Cek apakah member sudah punya role track wallet (hindari duplikat)
            track_wallet_role = guild.get_role(TRACK_WALLET_ROLE_ID)
            if not track_wallet_role:
                print(f"⚠️ Role track wallet dengan ID {TRACK_WALLET_ROLE_ID} tidak ditemukan")
                return
            
            if track_wallet_role in member.roles:
                print(f"[DEBUG] Member {member.name} sudah punya role track wallet, skip")
                return
            
            # Tambahkan role track wallet
            try:
                await member.add_roles(track_wallet_role)
                print(f"✅ {member.name} berhasil mengaktifkan fitur Track Wallet! Role {track_wallet_role.name} ditambahkan")
                
                # Kirim pesan konfirmasi ke channel fitur atau DM
                track_channel = bot.get_channel(TRACK_WALLET_CHANNEL_ID)
                confirm_msg = (
                    f"💼 **Fitur Track Wallet Diaktifkan!**\n\n"
                    f"Selamat {member.mention}! Kamu sekarang bisa akses **{track_channel.mention if track_channel else '#track-wallet'}**.\n"
                    f"Paste wallet Solana address kamu di sana untuk mulai tracking. (Fitur track akan diimplementasikan nanti)\n\n"
                    f"**Cara gunakan:**\n"
                    "1. Masuk ke channel track wallet\n"
                    "2. Ketik wallet address (akan auto-detect nanti)\n"
                    "3. Bot akan track transaksi & update! 📊"
                )
                
                try:
                    await member.send(confirm_msg)
                    print(f"[DEBUG] DM konfirmasi fitur dikirim ke {member.name}")
                except discord.Forbidden:
                    # Jika DM gagal, kirim ke channel fitur
                    await guild.get_channel(FEATURE_CHANNEL_ID).send(f"{member.mention} {confirm_msg.replace(member.mention, '')}")
                    print(f"[DEBUG] DM gagal, konfirmasi dikirim ke channel fitur untuk {member.name}")
                    
            except discord.Forbidden:
                print(f"❌ Bot tidak punya izin untuk mengubah role member {member.name}")
            except Exception as e:
                print(f"⚠️ Error saat mengaktifkan fitur untuk member {member.name}: {e}")
                import traceback
                traceback.print_exc()
                
        except Exception as e:
            print(f"[ERROR] Unexpected error in on_raw_reaction_add (feature): {e}")
            import traceback
            traceback.print_exc()

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