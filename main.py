import discord
from discord.ext import commands
import requests
import os
import re

# --- TOKEN ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if not TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

# --- DISCORD INTENTS ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # penting untuk event on_member_join
bot = commands.Bot(command_prefix='!', intents=intents)

# --- GANTI DENGAN CHANNEL & ROLE ID KAMU ---
ALLOWED_CHANNEL_ID = 1428299549507584080  # Channel LP Calls
MENTION_ROLE_ID = 1437345814245801994  # Role yg mau di-mention di thread
AUTO_ROLE_ID = 1437345814245801994  # 🟢 Role default untuk member baru
WELCOME_CHANNEL_ID = 1425708221175173125  # (Opsional) ID channel welcome


# --- EVENT: BOT ONLINE ---
@bot.event
async def on_ready():
    print(f"✅ {bot.user} sudah online dan siap digunakan!")


# --- EVENT: MEMBER BARU JOIN ---
@bot.event
async def on_member_join(member: discord.Member):
    role = member.guild.get_role(AUTO_ROLE_ID)
    if role:
        try:
            await member.add_roles(role)
            print(f"✅ Berhasil menambahkan role {role.name} ke {member.name}")
        except discord.Forbidden:
            print(
                f"❌ Bot tidak punya izin untuk menambahkan role di {member.guild.name}"
            )
        except Exception as e:
            print(f"⚠️ Error saat memberi role otomatis: {e}")
    else:
        print(
            f"⚠️ Role dengan ID {AUTO_ROLE_ID} tidak ditemukan di server {member.guild.name}"
        )

    # Kirim pesan sambutan (opsional)
    if WELCOME_CHANNEL_ID:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            await channel.send(
                f"👋 Selamat datang {member.mention}! "
                f"Role **{role.name if role else 'Default'}** sudah diberikan otomatis 🎉"
            )


# --- HELPER: CEK VALID SOLANA ADDRESS ---
def is_valid_solana_address(addr: str):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', addr))


# --- HELPER: FETCH POOL DATA ---
def fetch_meteora_pools(ca: str):
    url = 'https://dlmm-api.meteora.ag/pair/all?include_unknown=true'
    response = requests.get(url)
    response.raise_for_status()
    pools = response.json()

    matching_pools = []
    for pool in pools:
        mint_x = pool.get('mint_x', '').lower()
        mint_y = pool.get('mint_y', '').lower()
        if ca.lower() in [mint_x, mint_y]:
            name = pool.get('name', '').strip()
            if name:
                clean_name = name.replace(' DLMM', '').replace('DLMM',
                                                               '').strip()
                separator = '/' if '/' in clean_name else '-'
                parts = clean_name.split(separator)
                if len(parts) >= 2:
                    pair_name = f"{parts[0].strip()}-{parts[1].strip()}"
                else:
                    pair_name = clean_name
            else:
                matching_mint = mint_x if ca.lower() == mint_x else mint_y
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

    return matching_pools


# --- AUTO DETECT: USER PASTE CONTRACT ADDRESS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if is_valid_solana_address(content):
        await message.channel.send(
            f"🔍 Cek pool DLMM untuk token: `{content[:8]}...`")

        try:
            pools = fetch_meteora_pools(content)
            if not pools:
                embed = discord.Embed(
                    title="Pool DLMM Meteora",
                    description=
                    f"Gak ditemuin pool untuk token `{content[:8]}...`",
                    color=0xff0000)
                await message.channel.send(embed=embed)
                return

            pools.sort(key=lambda x: x['raw_liq'], reverse=True)
            desc = f"Found {len(pools)} pool untuk `{content}`\n\n"
            for i, p in enumerate(pools[:10], 1):
                link = f"https://app.meteora.ag/dlmm/{p['address']}"
                desc += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"

            embed = discord.Embed(title="Meteora Pool Bot",
                                  description=desc,
                                  color=0x00ff00)
            embed.set_footer(
                text=f"Requested by {message.author.display_name}")
            await message.channel.send(embed=embed)

        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")

    await bot.process_commands(message)


# --- COMMAND: !call <contract_address> ---
@bot.command(name="call")
@commands.has_any_role("Moderator", "admin")
async def call_token(ctx: commands.Context, ca: str):
    """Buat thread baru untuk token Meteora di mana saja, dan kirim link-nya ke channel LP Calls"""
    lp_calls_channel = bot.get_channel(ALLOWED_CHANNEL_ID)

    if not lp_calls_channel:
        await ctx.send(
            "❌ Gagal menemukan channel LP Calls. Cek ALLOWED_CHANNEL_ID.")
        return

    if not is_valid_solana_address(ca):
        await ctx.send("⚠️ Invalid Solana address!")
        return

    await ctx.send(f"🔍 Fetching Meteora DLMM pools for `{ca[:8]}...`")

    try:
        pools = fetch_meteora_pools(ca)
        if not pools:
            await ctx.send(f"Gak ditemuin pool untuk `{ca}`")
            return

        pools.sort(key=lambda x: x['raw_liq'], reverse=True)
        top_pool = pools[0]
        pair_name = top_pool['pair'].replace(" ", "")
        thread_name = f"{pair_name}"

        # 🧵 Buat thread di channel tempat command diketik
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

        # ✨ Kirim contract address dulu
        await thread.send(
            f"{mention_text} 💬 Thread created for `{pair_name}`\n\n"
            f"**Contract Address:** `{ca}`\n"
            f"https://solscan.io/token/{ca}")

        # Baru kirim embed pool
        await thread.send(embed=embed)

        # 📩 Kirim link thread ke channel LP Calls
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

    except discord.Forbidden:
        await ctx.send(
            "❌ Bot tidak punya izin untuk buat thread atau kirim pesan di sini."
        )
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


# --- RUN ---
bot.run(TOKEN)
