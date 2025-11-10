import discord
from discord.ext import commands
import os

# --- TOKEN ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
print(f"[DEBUG] Loaded TOKEN? {'✅ Yes' if TOKEN else '❌ No'}")

if not TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

# --- DISCORD INTENTS ---
intents = discord.Intents.default()
intents.members = True  # penting untuk event on_member_join
intents.guilds = True
print("[DEBUG] Discord intents sudah diaktifkan")

bot = commands.Bot(command_prefix='!', intents=intents)

# --- GANTI DENGAN CHANNEL & ROLE ID KAMU ---
AUTO_ROLE_ID = 1437345814245801994  # 🟢 Role default untuk member baru
WELCOME_CHANNEL_ID = 1425708221175173125  # ID channel welcome

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

# --- RUN BOT ---
print("[DEBUG] Bot starting...")
bot.run(TOKEN)
