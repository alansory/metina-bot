import discord
from discord.ext import commands, tasks
import requests
import aiohttp
import asyncio
import os
import re
import sys
import json
import time
from discord import app_commands
from typing import Dict, List, Optional
from datetime import datetime

# --- TOKEN ---
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
print(f"[DEBUG] Loaded TOKEN? {'✅ Yes' if TOKEN else '❌ No'}")

if not TOKEN:
    print("❌ ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

# --- HELIUS RPC CONFIG ---
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY')
if not HELIUS_API_KEY:
    print("⚠️ HELIUS_API_KEY not set - Wallet tracking will be disabled!")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# --- DISCORD INTENTS ---
intents = discord.Intents.default()
intents.message_content = True  # PENTING: untuk baca message content
intents.guilds = True
intents.members = True  # penting untuk event on_member_join
print("[DEBUG] Discord intents sudah diaktifkan")

bot = commands.Bot(command_prefix='!', intents=intents)

# --- AIOHTTP SESSION FOR ASYNC HTTP REQUESTS ---
http_session: Optional[aiohttp.ClientSession] = None

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

# --- DATA STORAGE UNTUK TRACKED WALLETS (per user) ---
TRACKED_WALLETS_FILE = 'tracked_wallets.json'
tracked_wallets = {}  # {user_id: {wallet: {'alias': 'nama', 'last_sig': None}}}

# --- GLOBAL DEFAULT TRACKED WALLETS (role-wide alerts) ---
DEFAULT_WALLETS_FILE = 'default_wallets.json'
default_tracked_wallets: List[Dict[str, Optional[str]]] = []  # [{'wallet': str, 'alias': str, 'last_sig': Optional[str]}]

def load_tracked_wallets():
    global tracked_wallets
    try:
        if os.path.exists(TRACKED_WALLETS_FILE):
            with open(TRACKED_WALLETS_FILE, 'r') as f:
                tracked_wallets = json.load(f)
            print(f"[DEBUG] Loaded {len(tracked_wallets)} users' tracked wallets")
    except Exception as e:
        print(f"[ERROR] Failed to load tracked wallets: {e}")
        tracked_wallets = {}

def save_tracked_wallets():
    try:
        with open(TRACKED_WALLETS_FILE, 'w') as f:
            json.dump(tracked_wallets, f, indent=4)
        print("[DEBUG] Saved tracked wallets")
    except Exception as e:
        print(f"[ERROR] Failed to save tracked wallets: {e}")

def load_default_wallets():
    """Load global default tracked wallets list (for role-wide notifications)."""
    global default_tracked_wallets
    try:
        if os.path.exists(DEFAULT_WALLETS_FILE):
            with open(DEFAULT_WALLETS_FILE, 'r') as f:
                data = json.load(f)
                # normalize structure
                normalized = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    wallet = item.get('wallet')
                    alias = item.get('alias') or (wallet[:8] + '...') if wallet else None
                    last_sig = item.get('last_sig') if isinstance(item.get('last_sig'), str) else None
                    # Validate wallet format directly to avoid early dependency issues
                    if wallet and re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', wallet):
                        normalized.append({'wallet': wallet, 'alias': alias, 'last_sig': last_sig})
                default_tracked_wallets = normalized
            print(f"[DEBUG] Loaded {len(default_tracked_wallets)} default wallets")
        else:
            default_tracked_wallets = []
            print("[DEBUG] No default wallets file found")
    except Exception as e:
        print(f"[ERROR] Failed to load default wallets: {e}")
        default_tracked_wallets = []

def save_default_wallets():
    """Persist global default tracked wallets list."""
    try:
        with open(DEFAULT_WALLETS_FILE, 'w') as f:
            json.dump(default_tracked_wallets, f, indent=4)
        print("[DEBUG] Saved default wallets")
    except Exception as e:
        print(f"[ERROR] Failed to save default wallets: {e}")

# Load data on startup
load_tracked_wallets()
load_default_wallets()

# --- HELPER: CEK VALID SOLANA WALLET ADDRESS ---
def is_valid_solana_wallet(addr: str):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', addr))

# --- HELPER CONSTS & UTILITIES ---
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_METADATA_TTL = 300  # seconds
token_metadata_cache: Dict[str, Dict[str, object]] = {}

def _parse_amount(value):
    """Convert various Helius amount representations to float (preserve sign)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    if isinstance(value, dict):
        for key in ("uiAmountString", "uiAmount", "tokenAmount", "amount"):
            if key in value and value[key] is not None:
                return _parse_amount(value[key])
    return 0.0

def _format_token_amount(amount_data):
    """Return human-readable token amount string from Helius payload."""
    if isinstance(amount_data, dict):
        if amount_data.get("uiAmountString"):
            return amount_data["uiAmountString"]
        if amount_data.get("uiAmount") is not None:
            return str(amount_data["uiAmount"])
        raw = amount_data.get("amount")
        decimals = amount_data.get("decimals")
        if raw is not None and decimals is not None:
            try:
                value = int(raw)
                return f"{value / (10 ** int(decimals)):.6f}".rstrip("0").rstrip(".")
            except (ValueError, TypeError):
                pass
    parsed = _parse_amount(amount_data)
    if parsed.is_integer():
        return str(int(parsed))
    return f"{parsed:.6f}".rstrip("0").rstrip(".")

def _get_token_in_transfer(tx: Dict, wallet: str):
    for transfer in tx.get("tokenTransfers", []):
        if transfer.get("toUserAccount") == wallet and _parse_amount(transfer.get("tokenAmount")) > 0:
            return transfer
    # fallback to token balance changes (sometimes tokenTransfers kosong)
    for change in tx.get("tokenBalanceChanges", []):
        if (
            change.get("userAccount") == wallet
            and change.get("mint") != SOL_MINT
            and _parse_amount(change.get("rawTokenAmount")) > 0
        ):
            return {
                "mint": change.get("mint"),
                "tokenAmount": change.get("rawTokenAmount"),
            }
    return None

def _calculate_sol_spent(tx: Dict, wallet: str) -> float:
    """Return SOL spent (positive float) for this swap."""
    lamports_spent = 0.0
    for transfer in tx.get("nativeTransfers", []):
        if transfer.get("fromUserAccount") == wallet:
            amount = _parse_amount(transfer.get("amount"))
            if amount > 0:
                lamports_spent += amount

    if lamports_spent == 0:
        for change in tx.get("tokenBalanceChanges", []):
            if change.get("userAccount") == wallet and change.get("mint") == SOL_MINT:
                amount = _parse_amount(change.get("rawTokenAmount"))
                if amount < 0:
                    lamports_spent += abs(amount)

    return lamports_spent / 1_000_000_000 if lamports_spent else 0.0

def _format_sol(amount: float) -> str:
    if amount <= 0:
        return "N/A"
    if amount >= 1:
        return f"{amount:.4f} ◎"
    return f"{amount:.6f} ◎"

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

async def fetch_token_metadata(mint: str) -> Dict[str, Optional[object]]:
    """Fetch token metadata (name, symbol, market cap) with simple caching."""
    now = time.time()
    cached = token_metadata_cache.get(mint)
    if cached and now - cached.get("timestamp", 0) < TOKEN_METADATA_TTL:
        return cached.get("data", {})

    metadata = {"name": None, "symbol": None, "market_cap": None}
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json()
            pairs = data.get("pairs") or []
            if pairs:
                # pilih pair dengan liquidity usd terbesar
                def liquidity_usd(pair: Dict) -> float:
                    liquidity = pair.get("liquidity") or {}
                    return float(liquidity.get("usd", 0) or 0)
                best_pair = max(pairs, key=liquidity_usd)
                base = best_pair.get("baseToken") or {}
                metadata["name"] = base.get("name")
                metadata["symbol"] = base.get("symbol")
                metadata["market_cap"] = (
                    best_pair.get("fdv")
                    or best_pair.get("marketCap")
                    or base.get("marketCap")
                )
    except Exception as e:
        print(f"[ERROR] Failed to fetch token metadata for {mint}: {e}")

    token_metadata_cache[mint] = {"timestamp": now, "data": metadata}
    return metadata

# --- HELPER: FETCH RECENT SWAPS FROM HELIUS ---
async def fetch_recent_swaps(wallet: str) -> List[Dict]:
    """Fetch most recent SWAP transactions (newest-first) without paginating backwards."""
    if not HELIUS_API_KEY:
        return []
    
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    params = {
        'api-key': HELIUS_API_KEY,
        'type': 'SWAP',
        'limit': 5,  # cek transaksi terbaru saja
    }
    
    try:
        async with http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json()
            return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch swaps for {wallet}: {e}")
        return []

# --- HELPER: DETECT BUY TRANSACTION ---
def is_buy_transaction(tx: Dict, wallet: str) -> bool:
    """Detect buy: wallet spends SOL (or SOL balance decreases) and receives new token."""
    if tx.get('type') != 'SWAP':
        return False
    
    # Cek native transfers: SOL out from wallet
    native_transfers = tx.get('nativeTransfers', [])
    sol_out = any(
        t.get('fromUserAccount') == wallet and _parse_amount(t.get('amount')) > 0
        for t in native_transfers
    )

    if not sol_out:
        for change in tx.get('tokenBalanceChanges', []):
            if change.get('userAccount') == wallet and change.get('mint') == SOL_MINT:
                if _parse_amount(change.get('rawTokenAmount')) < 0:
                    sol_out = True
                    break
    
    # Cek token transfers: New token in to wallet
    token_transfer = _get_token_in_transfer(tx, wallet)
    token_in = token_transfer is not None
    
    return sol_out and token_in

# --- HELPER: SEND BUY NOTIFICATION ---
async def send_buy_notification(user: discord.User, wallet_data: Dict):
    """Send DM or channel notification for buy event"""
    wallet = wallet_data['wallet']
    alias = wallet_data['alias']
    last_sig = wallet_data['last_sig']
    swaps = await fetch_recent_swaps(wallet)
    if not swaps:
        return
    
    # Inisialisasi pointer pertama kali: jangan spam notifikasi lama
    if last_sig is None:
        wallet_data['last_sig'] = swaps[0].get('signature')
        save_tracked_wallets()
        return

    # Kumpulkan tx yang lebih baru dari last_sig (data dari Helius: newest-first)
    new_txs = []
    for tx in swaps:
        sig = tx.get('signature')
        if sig == last_sig:
            break
        new_txs.append(tx)

    if not new_txs:
        return

    # Hanya notif untuk transaksi baru (fresh) dalam window singkat
    now_ts = time.time()
    FRESH_WINDOW_SECONDS = 120  # 2 menit

    for tx in new_txs:  # urutan newest -> older (dari API)
        if not is_buy_transaction(tx, wallet):
            continue
        tx_ts = tx.get('timestamp', now_ts)
        if now_ts - tx_ts > FRESH_WINDOW_SECONDS:
            continue  # skip yang sudah terlalu lama

        signature = tx.get('signature', 'Unknown')
        timestamp = tx_ts
        description = tx.get('description', 'Buy transaction detected')
        token_transfer = _get_token_in_transfer(tx, wallet)
        token_mint = token_transfer.get('mint') if token_transfer else 'Unknown'

        token_info = await fetch_token_metadata(token_mint) if token_mint and token_mint != 'Unknown' else {}
        token_name = token_info.get('name') or token_mint[:8] + "..."
        token_symbol = token_info.get('symbol')
        market_cap_str = _format_usd(token_info.get('market_cap') if token_info else None)

        sol_spent = _calculate_sol_spent(tx, wallet)
        sol_spent_str = _format_sol(sol_spent)

        links = []
        if token_mint and token_mint != 'Unknown':
            links.append(f"[Jupiter](https://jup.ag/tokens/{token_mint})")
            links.append(f"[GMGN](https://gmgn.ai/sol/token/{token_mint})")
        links_text = "\n".join(links) if links else "N/A"
        
        embed = discord.Embed(
            title="🛒 Buy Detected!",
            description=description,
            color=0x00ff00,
            timestamp=datetime.fromtimestamp(timestamp)
        )
        embed.add_field(name="Wallet", value=f"**{alias}**\n[GMGN](https://gmgn.ai/sol/address/{wallet})", inline=True)
        token_field_value = f"**{token_name}**"
        if token_symbol:
            token_field_value += f" ({token_symbol})"
        token_field_value += f"\n`{token_mint[:8]}...`"
        embed.add_field(name="Token", value=token_field_value, inline=True)
        embed.add_field(name="SOL Spent", value=sol_spent_str, inline=True)
        embed.add_field(name="Market Cap", value=market_cap_str, inline=True)
        embed.add_field(name="Tx", value=f"[View Tx](https://solscan.io/tx/{signature})", inline=True)
        embed.add_field(name="Links", value=links_text, inline=False)
        
        try:
            await user.send(embed=embed)
            print(f"[DEBUG] Buy notification sent to {user.name} for {signature}")
        except discord.Forbidden:
            # Fallback to tracker channel
            channel = bot.get_channel(TRACK_WALLET_CHANNEL_ID)
            if channel:
                role_mention = f"<@&{TRACK_WALLET_ROLE_ID}>" if TRACK_WALLET_ROLE_ID else ""
                mention_text = f"{user.mention} {role_mention}".strip()
                await channel.send(mention_text, embed=embed)
        
        # Update last_sig ke signature terbaru yang diproses
        wallet_data['last_sig'] = signature
        save_tracked_wallets()
        return

    # Tidak ada buy baru yang fresh, tetap majukan pointer ke paling baru untuk hindari spam lama
    wallet_data['last_sig'] = swaps[0].get('signature')
    save_tracked_wallets()

async def send_buy_notification_global(wallet_data: Dict):
    """Send channel notification (role-wide) for buy event from default wallets."""
    wallet = wallet_data['wallet']
    alias = wallet_data['alias']
    last_sig = wallet_data.get('last_sig')
    swaps = await fetch_recent_swaps(wallet)
    if not swaps:
        return
    
    channel = bot.get_channel(TRACK_WALLET_CHANNEL_ID)
    if not channel:
        return
    
    # Inisialisasi pointer pertama kali
    if last_sig is None:
        wallet_data['last_sig'] = swaps[0].get('signature')
        save_default_wallets()
        return

    new_txs = []
    for tx in swaps:
        sig = tx.get('signature')
        if sig == last_sig:
            break
        new_txs.append(tx)

    if not new_txs:
        return

    now_ts = time.time()
    FRESH_WINDOW_SECONDS = 120

    for tx in new_txs:
        if not is_buy_transaction(tx, wallet):
            continue
        tx_ts = tx.get('timestamp', now_ts)
        if now_ts - tx_ts > FRESH_WINDOW_SECONDS:
            continue

        signature = tx.get('signature', 'Unknown')
        timestamp = tx_ts
        description = tx.get('description', 'Buy transaction detected')
        token_transfer = _get_token_in_transfer(tx, wallet)
        token_mint = token_transfer.get('mint') if token_transfer else 'Unknown'

        token_info = await fetch_token_metadata(token_mint) if token_mint and token_mint != 'Unknown' else {}
        token_name = token_info.get('name') or token_mint[:8] + "..."
        token_symbol = token_info.get('symbol')
        market_cap_str = _format_usd(token_info.get('market_cap') if token_info else None)

        sol_spent = _calculate_sol_spent(tx, wallet)
        sol_spent_str = _format_sol(sol_spent)

        links = []
        if token_mint and token_mint != 'Unknown':
            links.append(f"[Jupiter](https://jup.ag/tokens/{token_mint})")
            links.append(f"[GMGN](https://gmgn.ai/sol/token/{token_mint})")
        links_text = "\n".join(links) if links else "N/A"
        
        embed = discord.Embed(
            title="🛒 Buy Detected!",
            description=description,
            color=0x00ff00,
            timestamp=datetime.fromtimestamp(timestamp)
        )
        embed.add_field(name="Wallet", value=f"**{alias}**\n[GMGN](https://gmgn.ai/sol/address/{wallet})", inline=True)
        token_field_value = f"**{token_name}**"
        if token_symbol:
            token_field_value += f" ({token_symbol})"
        token_field_value += f"\n`{token_mint[:8]}...`"
        embed.add_field(name="Token", value=token_field_value, inline=True)
        embed.add_field(name="SOL Spent", value=sol_spent_str, inline=True)
        embed.add_field(name="Market Cap", value=market_cap_str, inline=True)
        embed.add_field(name="Tx", value=f"[View Tx](https://solscan.io/tx/{signature})", inline=True)
        embed.add_field(name="Links", value=links_text, inline=False)

        role_mention = f"<@&{TRACK_WALLET_ROLE_ID}>" if TRACK_WALLET_ROLE_ID else ""
        await channel.send(role_mention, embed=embed)
        print(f"[DEBUG] Global buy notification sent for {signature}")

        wallet_data['last_sig'] = signature
        save_default_wallets()
        return

    wallet_data['last_sig'] = swaps[0].get('signature')
    save_default_wallets()

# --- BACKGROUND TASK: POLL FOR BUYS ---
@tasks.loop(minutes=1)  # Poll every 1 minute
async def poll_wallet_buys():
    if not HELIUS_API_KEY:
        print("[DEBUG] Skipping poll - No Helius API key")
        return
    
    # Poll user wallets
    for user_id_str, wallets_data in tracked_wallets.items():
        try:
            user = await bot.fetch_user(int(user_id_str))
            for wallet, wallet_data in wallets_data.items():
                try:
                    await send_buy_notification(user, {'wallet': wallet, 'alias': wallet_data['alias'], 'last_sig': wallet_data['last_sig']})
                    # Small delay to prevent overwhelming the API
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"[ERROR] Poll error for wallet {wallet[:8]}... of user {user_id_str}: {e}")
                    continue
        except Exception as e:
            print(f"[ERROR] Poll error for user {user_id_str}: {e}")
            continue
    
    # Poll global default wallets (role-wide)
    for item in default_tracked_wallets:
        try:
            await send_buy_notification_global(item)
            # Small delay to prevent overwhelming the API
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[ERROR] Poll error for default wallet {item.get('wallet', 'unknown')[:8]}...: {e}")
            continue

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
    global http_session
    print(f"✅ {bot.user} sudah online dan siap digunakan!")
    print(f"[DEBUG] Connected to {len(bot.guilds)} guild(s): {[g.name for g in bot.guilds]}")
    
    # Initialize aiohttp session
    if not http_session:
        http_session = aiohttp.ClientSession()
        print("[DEBUG] Initialized aiohttp session")
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"[DEBUG] Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[ERROR] Failed to sync slash commands: {e}")
    
    # Setup pesan verifikasi
    await setup_verify_message()
    
    # Setup pesan fitur (BARU)
    await setup_feature_message()
    
    # Start polling task if Helius key available
    if HELIUS_API_KEY:
        poll_wallet_buys.start()
        print("[DEBUG] Wallet buy polling started")

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

# --- SLASH COMMANDS UNTUK TRACK WALLET ---
@bot.tree.command(name="add_wallet", description="Tambah wallet address untuk tracking (hanya buy transactions)")
@app_commands.describe(wallet="Solana wallet address yang ingin di-track", alias="Optional alias/nama untuk wallet ini (misal: 'Main Wallet')")
async def add_wallet(interaction: discord.Interaction, wallet: str, alias: str = None):
    if not is_valid_solana_wallet(wallet):
        await interaction.response.send_message("❌ Invalid Solana wallet address! Pastikan formatnya benar (32-44 karakter base58).", ephemeral=True)
        return
    
    user_id = str(interaction.user.id)
    if user_id not in tracked_wallets:
        tracked_wallets[user_id] = {}
    
    if wallet in tracked_wallets[user_id]:
        await interaction.response.send_message(f"⚠️ Wallet `{wallet[:8]}...` sudah di-track sebelumnya!", ephemeral=True)
        return
    
    wallet_alias = alias if alias else f"{wallet[:8]}..."
    tracked_wallets[user_id][wallet] = {'alias': wallet_alias, 'last_sig': None}
    save_tracked_wallets()
    
    # Konfirmasi tracking (fokus buy only)
    embed = discord.Embed(
        title="💼 Wallet Ditambahkan!",
        description=f"🔍 Wallet **{wallet_alias}** (`{wallet[:8]}...`) sekarang di-track **hanya untuk transaksi BELI (buy)**.\n\n**Status:** Aktif! Bot akan monitor buy transactions di Solana via Helius RPC.\n\nGunakan `/remove_wallet` untuk hapus.",
        color=0x00ff00
    )
    embed.add_field(name="Link Wallet", value=f"[GMGN](https://gmgn.ai/sol/address/{wallet})", inline=False)
    embed.set_footer(text=f"Wallet: {wallet}")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    print(f"[DEBUG] Added wallet {wallet} ({wallet_alias}) for user {interaction.user.name}")

@bot.tree.command(name="remove_wallet", description="Hapus wallet address dari tracking")
@app_commands.describe(wallet="Solana wallet address yang ingin dihapus")
async def remove_wallet(interaction: discord.Interaction, wallet: str):
    if not is_valid_solana_wallet(wallet):
        await interaction.response.send_message("❌ Invalid Solana wallet address!", ephemeral=True)
        return
    
    user_id = str(interaction.user.id)
    if user_id not in tracked_wallets or wallet not in tracked_wallets[user_id]:
        await interaction.response.send_message(f"⚠️ Wallet `{wallet[:8]}...` tidak ditemukan di list tracking kamu!", ephemeral=True)
        return
    
    del tracked_wallets[user_id][wallet]
    if not tracked_wallets[user_id]:
        del tracked_wallets[user_id]
    save_tracked_wallets()
    
    embed = discord.Embed(
        title="🗑️ Wallet Dihapus!",
        description=f"Wallet `{wallet[:8]}...` sudah dihapus dari tracking.\n\nTidak ada lagi monitoring untuk wallet ini.",
        color=0xff0000
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    print(f"[DEBUG] Removed wallet {wallet} for user {interaction.user.name}")

@bot.tree.command(name="list_wallets", description="Lihat list wallet yang sedang di-track")
async def list_wallets(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in tracked_wallets or not tracked_wallets[user_id]:
        await interaction.response.send_message("📝 Belum ada wallet yang di-track. Gunakan `/add_wallet` untuk mulai!", ephemeral=True)
        return
    
    wallets_list = "\n".join([f"• **{data['alias']}**: `{w[:8]}...` [GMGN](https://gmgn.ai/sol/address/{w})" for w, data in tracked_wallets[user_id].items()])
    embed = discord.Embed(
        title="📋 List Wallet Tracking (Buy Only)",
        description=f"Wallet kamu yang sedang di-track:\n{wallets_list}\n\n**Catatan:** Hanya transaksi BELI yang dimonitor via Helius RPC.",
        color=0x3498db
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    print(f"[DEBUG] Listed wallets for user {interaction.user.name}")

# --- AUTO DETECT: USER PASTE CONTRACT ADDRESS (DISABLE AUTO-TRACK UNTUK WALLET YANG UDAH DI-ADD) ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(f"[DEBUG] Message detected in #{message.channel}: {message.content[:40]}")

    content = message.content.strip()
    if is_valid_solana_address(content):
        # Cek apakah ini di channel tracker wallet DAN bukan wallet yang sudah di-add
        if message.channel.id == TRACK_WALLET_CHANNEL_ID:
            user_id = str(message.author.id)
            if user_id in tracked_wallets and content in tracked_wallets[user_id]:
                print(f"[DEBUG] Wallet {content} sudah di-track via command, skip auto-detect")
                return  # Skip auto-track jika sudah di-add via command
            
            # Handle sebagai wallet tracking (hanya jika belum di-add)
            try:
                embed = discord.Embed(
                    title="💼 Wallet Tracking",
                    description=f"🔍 Wallet `{content[:8]}...` terdeteksi.\n\n**Saran:** Gunakan `/add_wallet {content} <alias>` untuk track hanya buy transactions!\n\n*(Auto-track sementara dinonaktifkan untuk wallet manual)*",
                    color=0x00ff00
                )
                embed.add_field(name="Link Wallet", value=f"[GMGN](https://gmgn.ai/sol/address/{content})", inline=False)
                embed.set_footer(text=f"Wallet: {content}")
                await message.reply(embed=embed)
                print(f"[DEBUG] Suggested command for wallet {content} by {message.author}")
            except Exception as e:
                print(f"[ERROR] Error handling wallet suggest: {e}")
            return  # Exit setelah handle tracker channel
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
                    f"Gunakan `/add_wallet <address> <alias>` untuk track wallet (hanya buy transactions).\n\n"
                    f"**Cara gunakan:**\n"
                    "1. Masuk ke channel track wallet\n"
                    "2. Ketik `/add_wallet <address> <alias>`\n"
                    "3. Bot akan track buy transaksi & update! 📊"
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

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    # Handle removal of feature reaction to revoke role
    if payload.channel_id != FEATURE_CHANNEL_ID:
        return
    if str(payload.emoji) != TRACK_WALLET_EMOJI:
        return
    try:
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            print(f"⚠️ Guild dengan ID {payload.guild_id} tidak ditemukan")
            return
        
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                member = None
        if not member:
            print(f"⚠️ Member dengan ID {payload.user_id} tidak ditemukan")
            return
        
        track_wallet_role = guild.get_role(TRACK_WALLET_ROLE_ID)
        if not track_wallet_role:
            print(f"⚠️ Role track wallet dengan ID {TRACK_WALLET_ROLE_ID} tidak ditemukan")
            return
        
        if track_wallet_role in member.roles:
            try:
                await member.remove_roles(track_wallet_role, reason="User removed track wallet reaction")
                print(f"✅ Role {track_wallet_role.name} dihapus dari {member.name} karena menghapus reaction fitur")
            except discord.Forbidden:
                print(f"❌ Bot tidak punya izin untuk menghapus role dari {member.name}")
            except Exception as e:
                print(f"⚠️ Error saat menghapus role dari {member.name}: {e}")
    except Exception as e:
        print(f"[ERROR] Unexpected error in on_raw_reaction_remove (feature): {e}")
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

        # Buat embed untuk contract address dengan multiple links
        contract_embed = discord.Embed(
            title=f"💬 Thread created for `{pair_name}`",
            description=f"**Contract Address:** `{ca}`",
            color=0x3498db
        )
        contract_embed.add_field(
            name="🔗 Links",
            value=(
                f"[🔍 Solscan](https://solscan.io/token/{ca})\n"
                f"[🪐 Jupiter](https://jup.ag/tokens/{ca})\n"
                f"[📊 GMGN](https://gmgn.ai/sol/token/{ca})"
            ),
            inline=False
        )
        
        await thread.send(f"{mention_text}", embed=contract_embed)

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