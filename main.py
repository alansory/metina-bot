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
import random
from collections import deque
from discord import app_commands
from typing import Dict, List, Optional, Tuple
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

# --- RATE LIMITING & CIRCUIT BREAKER ---
# Rate limiter: max 8 requests per minute (Helius free tier biasanya 100/min, kita konservatif)
RATE_LIMIT_REQUESTS = 8  # Max requests per window
RATE_LIMIT_WINDOW = 60  # 60 seconds window
request_timestamps = deque()  # Track request timestamps
circuit_breaker_active = False
circuit_breaker_until = 0  # Timestamp when circuit breaker resets
MIN_DELAY_BETWEEN_REQUESTS = 8  # Minimum 8 seconds between requests (conservative)
MAX_DELAY_BETWEEN_REQUESTS = 12  # Max 12 seconds (with jitter)

# --- METEORA API RATE LIMITING ---
# Rate limiter untuk Meteora API (synchronous)
meteora_last_request_time = 0  # Timestamp of last Meteora API request
METEORA_MIN_DELAY = 3  # Minimum 3 seconds between Meteora requests
meteora_circuit_breaker_active = False
meteora_circuit_breaker_until = 0

# --- METADAO CONFIG ---
METADAO_PROJECTS_URL = "https://metadao.fi/projects"
METADAO_STATE_FILE = "metadao_projects_state.json"
METADAO_POLL_INTERVAL_MINUTES = int(os.getenv("METADAO_POLL_INTERVAL", "10"))
DAMM_CHANNEL_ID = int(os.getenv("DAMM_V2_CHANNEL_ID", "1440565218739486881")) or None
DAMM_CHANNEL_NAME = os.getenv("DAMM_V2_CHANNEL_NAME", "damm")
metadao_notification_state: Dict[str, Dict[str, object]] = {}

# --- THREAD AUTO-ARCHIVE CONFIG ---
THREAD_AUTO_ARCHIVE_MINUTES = 15  # Auto-archive thread setelah 15 menit
threads_to_archive: Dict[int, float] = {}  # {thread_id: created_timestamp}
AUTO_SCAN_OLD_THREADS_ON_STARTUP = os.getenv("AUTO_SCAN_OLD_THREADS", "false").lower() == "true"  # Set ke "true" untuk enable auto-scan saat startup

async def wait_for_rate_limit():
    """Wait if we're hitting rate limits, implements token bucket pattern."""
    global circuit_breaker_active, circuit_breaker_until, request_timestamps
    
    now = time.time()
    
    # Check circuit breaker
    if circuit_breaker_active:
        if now < circuit_breaker_until:
            wait_time = circuit_breaker_until - now
            print(f"[RATE_LIMIT] Circuit breaker active, waiting {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)
            circuit_breaker_active = False
        else:
            circuit_breaker_active = False
    
    # Clean old timestamps outside window
    while request_timestamps and now - request_timestamps[0] > RATE_LIMIT_WINDOW:
        request_timestamps.popleft()
    
    # If we're at the limit, wait until oldest request expires
    if len(request_timestamps) >= RATE_LIMIT_REQUESTS:
        oldest = request_timestamps[0]
        wait_time = RATE_LIMIT_WINDOW - (now - oldest) + 1  # +1 for safety
        print(f"[RATE_LIMIT] Rate limit reached ({len(request_timestamps)}/{RATE_LIMIT_REQUESTS}), waiting {wait_time:.1f}s...")
        await asyncio.sleep(wait_time)
        # Clean again after waiting
        now = time.time()
        while request_timestamps and now - request_timestamps[0] > RATE_LIMIT_WINDOW:
            request_timestamps.popleft()
    
    # Add jitter to avoid thundering herd
    jitter = random.uniform(0, 2)  # 0-2 seconds random delay
    base_delay = random.uniform(MIN_DELAY_BETWEEN_REQUESTS, MAX_DELAY_BETWEEN_REQUESTS)
    delay = base_delay + jitter
    
    await asyncio.sleep(delay)
    
    # Record this request
    request_timestamps.append(time.time())

def activate_circuit_breaker(duration: int = 300):
    """Activate circuit breaker for specified duration (default 5 minutes)."""
    global circuit_breaker_active, circuit_breaker_until
    circuit_breaker_active = True
    circuit_breaker_until = time.time() + duration
    print(f"[CIRCUIT_BREAKER] Activated for {duration}s due to rate limiting")

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

def load_metadao_state():
    """Load persisted MetaDAO notification state."""
    global metadao_notification_state
    try:
        if os.path.exists(METADAO_STATE_FILE):
            with open(METADAO_STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    metadao_notification_state = data
                    print(f"[DEBUG] Loaded MetaDAO state for {len(metadao_notification_state)} project(s)")
    except Exception as e:
        print(f"[ERROR] Failed to load MetaDAO notification state: {e}")
        metadao_notification_state = {}

def save_metadao_state():
    """Persist MetaDAO notification state to disk."""
    try:
        with open(METADAO_STATE_FILE, "w") as f:
            json.dump(metadao_notification_state, f, indent=4)
        print("[DEBUG] Saved MetaDAO notification state")
    except Exception as e:
        print(f"[ERROR] Failed to save MetaDAO notification state: {e}")

# Load data on startup
load_tracked_wallets()
load_default_wallets()
load_metadao_state()

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

def _extract_metadao_items(html: str) -> List[Dict[str, object]]:
    """Extract MetaDAO launch data blob from rendered HTML."""
    marker = '{"items":['
    start = html.find(marker)
    if start == -1:
        return []
    # Find matching closing brace for the JSON object
    depth = 0
    in_string = False
    escape = False
    end = None
    for idx in range(start, len(html)):
        ch = html[idx]
        if in_string:
            if ch == '"' and not escape:
                in_string = False
            escape = (ch == '\\' and not escape)
            continue
        if ch == '"':
            in_string = True
            escape = False
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        return []
    payload = html[start:end]
    try:
        data = json.loads(payload)
        items = data.get("items", [])
        if isinstance(items, list):
            return items
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to decode MetaDAO payload: {e}")
    return []

def _format_usd_short(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    thresholds = [
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ]
    for threshold, suffix in thresholds:
        if value >= threshold:
            return f"${value/threshold:.1f}{suffix}"
    return f"${value:,.0f}"

def _metadao_amount_to_usd(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    try:
        # MetaDAO stores USD amounts in millionths (per on-site displays)
        return float(value) / 1_000_000
    except (TypeError, ValueError):
        return None

async def fetch_metadao_launches() -> List[Dict[str, object]]:
    """Fetch active MetaDAO launches with remaining time."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    try:
        async with http_session.get(METADAO_PROJECTS_URL, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            html = await response.text()
    except Exception as e:
        print(f"[ERROR] Failed to fetch MetaDAO projects: {e}")
        return []

    items = _extract_metadao_items(html)
    active_launches = []
    now_ts = time.time()
    for item in items:
        remaining = item.get("timeRemaining") or {}
        total_seconds = remaining.get("total") if isinstance(remaining, dict) else None
        if not isinstance(total_seconds, (int, float)) or total_seconds <= 0:
            continue
        end_ts = now_ts + total_seconds
        launch = {
            "id": item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "token_symbol": item.get("tokenSymbol"),
            "price": item.get("price"),
            "buy_url": f"https://metadao.fi/projects/{item.get('organizationSlug')}/fundraise" if item.get("organizationSlug") else METADAO_PROJECTS_URL,
            "committed": _metadao_amount_to_usd(item.get("finalRaiseAmount")),
            "target": _metadao_amount_to_usd(item.get("minimumRaise")),
            "time_remaining": total_seconds,
            "end_ts": end_ts,
        }
        if launch["id"]:
            active_launches.append(launch)
    return active_launches

def _find_damm_channel() -> Optional[discord.TextChannel]:
    if DAMM_CHANNEL_ID:
        channel = bot.get_channel(DAMM_CHANNEL_ID)
        if channel:
            return channel  # type: ignore[return-value]
    if DAMM_CHANNEL_NAME:
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.name == DAMM_CHANNEL_NAME:
                    return channel  # type: ignore[return-value]
    return None

def _metadao_state_for(project_id: str) -> Dict[str, object]:
    state = metadao_notification_state.get(project_id)
    if not isinstance(state, dict):
        state = {}
    return state

def _user_can_run_admin_actions(user: discord.abc.User) -> bool:
    if isinstance(user, discord.Member):
        perms = user.guild_permissions
        return (
            perms.administrator
            or perms.manage_guild
            or perms.manage_channels
            or perms.manage_messages
        )
    return False

def _metadao_admin_check(interaction: discord.Interaction) -> bool:
    if _user_can_run_admin_actions(interaction.user):
        return True
    raise app_commands.CheckFailure("Kamu butuh izin Manage Server untuk pakai command ini.")

async def _send_metadao_embed(channel: discord.TextChannel, launch: Dict[str, object], *, reminder: bool):
    title = "🚀 New MetaDAO Raise Live" if not reminder else "⏰ MetaDAO Raise Ending Soon"
    end_dt = datetime.fromtimestamp(launch["end_ts"])
    time_left_minutes = max(0, int(launch["time_remaining"] // 60))
    desc_parts = []
    if launch.get("description"):
        desc_parts.append(launch["description"])
    desc_parts.append(f"Ends at **{end_dt:%Y-%m-%d %H:%M UTC}** ({time_left_minutes} min left)")
    embed = discord.Embed(
        title=title,
        description="\n\n".join(desc_parts),
        color=0xFF7B7B if reminder else 0x3498db,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Project", value=f"**{launch.get('name')}**", inline=True)
    if launch.get("token_symbol"):
        embed.add_field(name="Ticker", value=launch["token_symbol"], inline=True)
    price = launch.get("price")
    if isinstance(price, (int, float)):
        embed.add_field(name="Price", value=f"${price:.4f}", inline=True)
    committed = _format_usd_short(launch.get("committed"))
    target = _format_usd_short(launch.get("target"))
    embed.add_field(name="Raised", value=f"{committed} / {target}", inline=False)
    embed.add_field(name="MetaDAO", value=f"[Open Raise]({launch.get('buy_url')})", inline=False)
    await channel.send(embed=embed)

@tasks.loop(minutes=1)  # Check setiap 1 menit
async def auto_archive_threads():
    """Auto-archive thread setelah 15 menit dibuat"""
    global threads_to_archive
    if not threads_to_archive:
        return
    
    now = time.time()
    threads_to_remove = []
    
    for thread_id, created_at in list(threads_to_archive.items()):
        elapsed_minutes = (now - created_at) / 60
        
        if elapsed_minutes >= THREAD_AUTO_ARCHIVE_MINUTES:
            try:
                # Fetch thread
                thread = bot.get_channel(thread_id)
                if thread and isinstance(thread, discord.Thread):
                    if not thread.archived:
                        await thread.edit(archived=True, locked=False)
                        print(f"[DEBUG] Auto-archived thread {thread.name} (ID: {thread_id}) setelah {elapsed_minutes:.1f} menit")
                    threads_to_remove.append(thread_id)
                else:
                    # Thread tidak ditemukan atau sudah dihapus
                    threads_to_remove.append(thread_id)
            except discord.NotFound:
                # Thread sudah dihapus
                threads_to_remove.append(thread_id)
            except discord.Forbidden:
                print(f"[WARN] Tidak punya izin untuk archive thread {thread_id}")
                threads_to_remove.append(thread_id)
            except Exception as e:
                print(f"[ERROR] Gagal archive thread {thread_id}: {e}")
                # Jangan remove dari list jika error, biar dicoba lagi nanti
    
    # Remove thread yang sudah di-archive atau error
    for thread_id in threads_to_remove:
        threads_to_archive.pop(thread_id, None)

@tasks.loop(minutes=METADAO_POLL_INTERVAL_MINUTES or 10)
async def poll_metadao_launches():
    channel = _find_damm_channel()
    if not channel:
        print("[WARN] MetaDAO poll: damm-v2 channel not found")
        return
    launches = await fetch_metadao_launches()
    print(f"[DEBUG] MetaDAO poll: {len(launches)} active launch(es) detected")
    now_ts = time.time()
    active_ids = set()
    for launch in launches:
        project_id = launch["id"]
        if not project_id:
            continue
        active_ids.add(project_id)
        state = _metadao_state_for(project_id)
        prev_end = state.get("end_ts")
        end_changed = not isinstance(prev_end, (int, float)) or abs(prev_end - launch["end_ts"]) > 60
        if not state.get("start_notified") or end_changed:
            try:
                await _send_metadao_embed(channel, launch, reminder=False)
                state["start_notified"] = True
                state["reminder_sent"] = False
                print(f"[DEBUG] MetaDAO start notification sent for {project_id}")
            except Exception as e:
                print(f"[ERROR] Failed to send MetaDAO start notification: {e}")
        # Reminder logic
        if not state.get("reminder_sent") and launch["end_ts"] - now_ts <= 3600:
            try:
                await _send_metadao_embed(channel, launch, reminder=True)
                state["reminder_sent"] = True
                print(f"[DEBUG] MetaDAO reminder sent for {project_id}")
            except Exception as e:
                print(f"[ERROR] Failed to send MetaDAO reminder: {e}")
        state["end_ts"] = launch["end_ts"]
        state["time_remaining"] = launch["time_remaining"]
        metadao_notification_state[project_id] = state
    # Clean up stale entries so future raises can trigger notifications again
    for project_id in list(metadao_notification_state.keys()):
        if project_id not in active_ids:
            del metadao_notification_state[project_id]
    save_metadao_state()

# --- HELPER: FETCH RECENT SWAPS FROM HELIUS ---
async def fetch_recent_swaps(wallet: str, max_retries: int = 2) -> List[Dict]:
    """Fetch most recent SWAP transactions (newest-first) without paginating backwards.
    Implements exponential backoff for rate limiting (429 errors) with global rate limiter."""
    if not HELIUS_API_KEY:
        return []
    
    global http_session, circuit_breaker_active, circuit_breaker_until
    
    # Check circuit breaker first
    if circuit_breaker_active and time.time() < circuit_breaker_until:
        print(f"[SKIP] Circuit breaker active, skipping wallet {wallet[:8]}...")
        return []
    
    # Wait for rate limit before making request
    await wait_for_rate_limit()
    
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    params = {
        'api-key': HELIUS_API_KEY,
        'type': 'SWAP',
        'limit': 5,  # cek transaksi terbaru saja
    }
    
    consecutive_429s = 0
    
    for attempt in range(max_retries):
        try:
            async with http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                # Handle rate limiting (429) with exponential backoff
                if response.status == 429:
                    consecutive_429s += 1
                    retry_after = int(response.headers.get('Retry-After', 120))  # Default 2 minutes
                    wait_time = min(retry_after, 300)  # Cap at 5 minutes
                    
                    # If multiple 429s, activate circuit breaker
                    if consecutive_429s >= 2:
                        activate_circuit_breaker(duration=600)  # 10 minutes
                        print(f"[ERROR] Multiple 429 errors, circuit breaker activated for 10 minutes")
                        return []
                    
                    if attempt < max_retries - 1:
                        print(f"[WARN] Rate limited (429) for {wallet[:8]}... - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                        await asyncio.sleep(wait_time)
                        # Wait again for rate limit after backoff
                        await wait_for_rate_limit()
                        continue
                    else:
                        print(f"[ERROR] Rate limited (429) for {wallet[:8]}... - max retries reached, activating circuit breaker")
                        activate_circuit_breaker(duration=300)  # 5 minutes
                        return []
                
                # Reset consecutive 429s on success
                consecutive_429s = 0
                response.raise_for_status()
                data = await response.json()
                return data
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                consecutive_429s += 1
                # If multiple 429s, activate circuit breaker
                if consecutive_429s >= 2:
                    activate_circuit_breaker(duration=600)  # 10 minutes
                    print(f"[ERROR] Multiple 429 errors, circuit breaker activated for 10 minutes")
                    return []
                
                if attempt < max_retries - 1:
                    wait_time = 120 * (2 ** attempt)  # Exponential backoff: 120s, 240s
                    wait_time = min(wait_time, 300)  # Cap at 5 minutes
                    print(f"[WARN] Rate limited (429) for {wallet[:8]}... - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    await asyncio.sleep(wait_time)
                    await wait_for_rate_limit()
                    continue
                else:
                    print(f"[ERROR] Rate limited (429) for {wallet[:8]}... - max retries reached, activating circuit breaker")
                    activate_circuit_breaker(duration=300)  # 5 minutes
                    return []
            else:
                print(f"[ERROR] HTTP {e.status} error fetching swaps for {wallet[:8]}...: {e}")
                return []
        except Exception as e:
            print(f"[ERROR] Failed to fetch swaps for {wallet[:8]}...: {e}")
            return []
    
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
@tasks.loop(minutes=5)  # Poll every 5 minutes (increased to reduce rate limit issues)
async def poll_wallet_buys():
    if not HELIUS_API_KEY:
        print("[DEBUG] Skipping poll - No Helius API key")
        return
    
    global circuit_breaker_active, circuit_breaker_until
    
    # Check circuit breaker - skip entire cycle if active
    if circuit_breaker_active and time.time() < circuit_breaker_until:
        remaining = circuit_breaker_until - time.time()
        print(f"[SKIP] Polling skipped - Circuit breaker active for {remaining:.0f}s more")
        return
    
    # Count total wallets to track progress
    total_wallets = sum(len(wallets) for wallets in tracked_wallets.values()) + len(default_tracked_wallets)
    if total_wallets == 0:
        return
    
    print(f"[DEBUG] Polling {total_wallets} wallet(s) for buy transactions...")
    
    processed = 0
    skipped = 0
    
    # Poll user wallets
    for user_id_str, wallets_data in tracked_wallets.items():
        # Check circuit breaker before each user
        if circuit_breaker_active and time.time() < circuit_breaker_until:
            print(f"[SKIP] Stopping polling due to circuit breaker")
            break
            
        try:
            user = await bot.fetch_user(int(user_id_str))
            for wallet, wallet_data in wallets_data.items():
                # Check circuit breaker before each wallet
                if circuit_breaker_active and time.time() < circuit_breaker_until:
                    print(f"[SKIP] Stopping polling due to circuit breaker")
                    break
                    
                try:
                    await send_buy_notification(user, {'wallet': wallet, 'alias': wallet_data['alias'], 'last_sig': wallet_data['last_sig']})
                    processed += 1
                    # Rate limiting is handled in fetch_recent_swaps via wait_for_rate_limit()
                except Exception as e:
                    print(f"[ERROR] Poll error for wallet {wallet[:8]}... of user {user_id_str}: {e}")
                    skipped += 1
                    # If it's a rate limit error, wait a bit before continuing
                    if "429" in str(e) or "rate limit" in str(e).lower():
                        await asyncio.sleep(10)
                    continue
        except Exception as e:
            print(f"[ERROR] Poll error for user {user_id_str}: {e}")
            skipped += 1
            continue
    
    # Poll global default wallets (role-wide) - only if circuit breaker not active
    if not (circuit_breaker_active and time.time() < circuit_breaker_until):
        for item in default_tracked_wallets:
            # Check circuit breaker before each wallet
            if circuit_breaker_active and time.time() < circuit_breaker_until:
                print(f"[SKIP] Stopping polling due to circuit breaker")
                break
                
            try:
                await send_buy_notification_global(item)
                processed += 1
                # Rate limiting is handled in fetch_recent_swaps via wait_for_rate_limit()
            except Exception as e:
                print(f"[ERROR] Poll error for default wallet {item.get('wallet', 'unknown')[:8]}...: {e}")
                skipped += 1
                # If it's a rate limit error, wait a bit before continuing
                if "429" in str(e) or "rate limit" in str(e).lower():
                    await asyncio.sleep(10)
                continue
    
    print(f"[DEBUG] Completed polling cycle: {processed} processed, {skipped} skipped")

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

# --- HELPER: SCAN & ARCHIVE OLD THREADS ---
async def scan_and_archive_old_threads():
    """Scan thread lama di channel LP Calls dan archive yang sudah lebih dari 15 menit"""
    global threads_to_archive
    
    lp_calls_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    if not lp_calls_channel:
        print(f"[WARN] Channel LP Calls (ID: {ALLOWED_CHANNEL_ID}) tidak ditemukan, skip scan thread lama")
        return
    
    try:
        # Fetch semua active threads di channel
        active_threads = lp_calls_channel.threads
        
        now = time.time()
        scanned_count = 0
        archived_count = 0
        
        # Scan active threads
        for thread in active_threads:
            if not isinstance(thread, discord.Thread):
                continue
            
            scanned_count += 1
            
            # Skip thread yang sudah archived
            if thread.archived:
                continue
            
            # Cek umur thread (created_at timestamp)
            if not thread.created_at:
                # Jika tidak ada created_at, anggap thread baru dan tambahkan ke tracking
                threads_to_archive[thread.id] = now
                print(f"[DEBUG] Added thread '{thread.name}' to tracking (no created_at, akan di-archive dalam {THREAD_AUTO_ARCHIVE_MINUTES} menit)")
                continue
            
            thread_age_seconds = (now - thread.created_at.timestamp())
            thread_age_minutes = thread_age_seconds / 60
            
            # Jika thread sudah lebih dari 15 menit, langsung archive
            if thread_age_minutes >= THREAD_AUTO_ARCHIVE_MINUTES:
                try:
                    await thread.edit(archived=True, locked=False)
                    archived_count += 1
                    print(f"[DEBUG] Archived old thread '{thread.name}' (umur: {thread_age_minutes:.1f} menit)")
                except discord.Forbidden:
                    print(f"[WARN] Tidak punya izin untuk archive thread {thread.id}")
                except Exception as e:
                    print(f"[ERROR] Gagal archive thread {thread.id}: {e}")
            else:
                # Thread masih baru, tambahkan ke tracking untuk auto-archive nanti
                remaining_minutes = THREAD_AUTO_ARCHIVE_MINUTES - thread_age_minutes
                # Simpan dengan timestamp yang sudah adjusted
                threads_to_archive[thread.id] = now - (thread_age_minutes * 60)
                print(f"[DEBUG] Added existing thread '{thread.name}' to tracking (akan di-archive dalam {remaining_minutes:.1f} menit)")
        
        print(f"[DEBUG] Scan thread lama selesai: {scanned_count} thread scanned, {archived_count} thread di-archive")
        
    except Exception as e:
        print(f"[ERROR] Error saat scan thread lama: {e}")
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
    
    if not poll_metadao_launches.is_running():
        poll_metadao_launches.start()
        print("[DEBUG] MetaDAO polling started")
    
    # Scan dan archive thread lama yang sudah ada (hanya jika flag enabled)
    if AUTO_SCAN_OLD_THREADS_ON_STARTUP:
        print("[DEBUG] Auto-scan thread lama enabled, scanning...")
        await scan_and_archive_old_threads()
    else:
        print("[DEBUG] Auto-scan thread lama disabled (set AUTO_SCAN_OLD_THREADS=true untuk enable)")
    
    # Start auto-archive thread task
    if not auto_archive_threads.is_running():
        auto_archive_threads.start()
        print("[DEBUG] Thread auto-archive task started (15 menit)")

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
def fetch_meteora_pools(ca: str, max_retries: int = 3):
    """Fetch Meteora pools with rate limiting and retry logic for 429 errors."""
    global meteora_last_request_time, meteora_circuit_breaker_active, meteora_circuit_breaker_until
    
    print(f"[DEBUG] Fetching Meteora pools for {ca} using all_by_groups API")
    base_url = 'https://dlmm-api.meteora.ag/pair/all_by_groups'
    
    # OPTIMASI: Gunakan search_term untuk filter di server side, jauh lebih cepat!
    # API akan filter pools yang mengandung contract address ini
    target_contract = ca
    
    # Check circuit breaker
    now = time.time()
    if meteora_circuit_breaker_active and now < meteora_circuit_breaker_until:
        remaining = meteora_circuit_breaker_until - now
        print(f"[METEORA] Circuit breaker active, waiting {remaining:.1f}s...")
        raise Exception(f"API sedang rate limited. Coba lagi dalam {int(remaining)} detik.")
    
    # Rate limiting: ensure minimum delay between requests
    if meteora_last_request_time > 0:
        time_since_last = now - meteora_last_request_time
        if time_since_last < METEORA_MIN_DELAY:
            wait_time = METEORA_MIN_DELAY - time_since_last
            print(f"[METEORA] Rate limiting: waiting {wait_time:.1f}s before request...")
            time.sleep(wait_time)
            now = time.time()
    
    params = {
        'search_term': target_contract,  # Filter by contract address
        'sort_key': 'tvl',  # Sort by TVL untuk dapat pools teratas
        'order_by': 'desc',  # Descending order (highest TVL first)
        'limit': 50 # Ambil 50 pools teratas (cukup untuk sort & ambil top 10)
    }
    
    # Retry logic with exponential backoff for 429 errors
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            print(f"[DEBUG] Using all_by_groups API: {base_url} (attempt {attempt + 1}/{max_retries})")
            print(f"[DEBUG] Search term: {target_contract}")
            sys.stdout.flush()
            
            print(f"[DEBUG] Making request with search_term...")
            sys.stdout.flush()
            
            response = requests.get(base_url, params=params, timeout=30)
            
            # Handle 429 (Too Many Requests) with retry
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))  # Default 60 seconds
                wait_time = min(retry_after, 120)  # Cap at 2 minutes
                
                if attempt < max_retries - 1:
                    print(f"[METEORA] Rate limited (429) - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(wait_time)
                    continue
                else:
                    # Activate circuit breaker on final failure
                    meteora_circuit_breaker_active = True
                    meteora_circuit_breaker_until = time.time() + wait_time
                    print(f"[METEORA] Max retries reached, activating circuit breaker for {wait_time}s")
                    raise Exception(f"API rate limited. Coba lagi dalam {wait_time} detik.")
            
            response.raise_for_status()
            data = response.json()
            
            # Update last request time on success
            meteora_last_request_time = time.time()
            
            # Reset circuit breaker on success
            meteora_circuit_breaker_active = False
            
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
            
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 429:
                # Already handled above, but catch here for safety
                if attempt < max_retries - 1:
                    wait_time = 60 * (2 ** attempt)  # Exponential backoff: 60s, 120s, 240s
                    wait_time = min(wait_time, 120)  # Cap at 2 minutes
                    print(f"[METEORA] Rate limited (429) - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(wait_time)
                    continue
                else:
                    meteora_circuit_breaker_active = True
                    meteora_circuit_breaker_until = time.time() + wait_time
                    raise Exception(f"API rate limited setelah {max_retries} percobaan. Coba lagi dalam {wait_time} detik.")
            else:
                print(f"[ERROR] HTTP error: {e}")
                raise Exception(f"HTTP error: {e}")
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_time = 10 * (attempt + 1)  # 10s, 20s, 30s
                print(f"[METEORA] Request timeout - retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            print("[ERROR] Request timeout - API tidak merespons dalam 30 detik")
            raise Exception("Request timeout - API tidak merespons. Coba lagi nanti.")
        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                wait_time = 5 * (attempt + 1)  # 5s, 10s, 15s
                print(f"[METEORA] Connection error - retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            print(f"[ERROR] Connection error: {e}")
            raise Exception(f"Connection error: Tidak bisa connect ke API. {str(e)}")
        except Exception as e:
            # Don't retry on other exceptions
            print(f"[ERROR] Unexpected error in fetch_meteora_pools: {e}")
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            raise
    
    # Should not reach here, but just in case
    raise Exception("Gagal fetch pools setelah beberapa percobaan. Coba lagi nanti.")

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

@bot.tree.command(name="metadao_test", description="Kirim notifikasi MetaDAO test ke channel damm")
@app_commands.describe(
    project_name="Nama project/raise",
    minutes_until_end="Berapa menit lagi raise berakhir",
    reminder="Kirim versi reminder (1 jam sebelum akhir)",
    token_symbol="Ticker/token symbol (opsional)",
    price="Harga token (opsional)",
    raised="Dana yang sudah terkumpul (USD, opsional)",
    target="Target raise (USD, opsional)",
)
@app_commands.check(_metadao_admin_check)
async def metadao_test(
    interaction: discord.Interaction,
    project_name: str,
    minutes_until_end: int,
    reminder: bool = False,
    token_symbol: Optional[str] = None,
    price: Optional[float] = None,
    raised: Optional[float] = None,
    target: Optional[float] = None,
):
    channel = _find_damm_channel()
    if not channel:
        await interaction.response.send_message("❌ Channel damm-v2 tidak ditemukan. Cek konfigurasi ID/Nama channel!", ephemeral=True)
        return
    
    minutes = max(1, minutes_until_end)
    end_ts = time.time() + minutes * 60
    launch = {
        "id": f"TEST-{int(time.time())}",
        "name": project_name,
        "description": "Test notification (tidak berasal dari MetaDAO).",
        "token_symbol": token_symbol,
        "price": price,
        "buy_url": METADAO_PROJECTS_URL,
        "committed": raised,
        "target": target,
        "time_remaining": minutes * 60,
        "end_ts": end_ts,
    }
    
    try:
        await _send_metadao_embed(channel, launch, reminder=reminder)
        await interaction.response.send_message(f"✅ Notif MetaDAO test dikirim ke {channel.mention}", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Failed to send MetaDAO test notification: {e}")
        await interaction.response.send_message(f"❌ Gagal kirim notif: {e}", ephemeral=True)

@metadao_test.error
async def metadao_test_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menjalankan command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

@bot.tree.command(name="close_thread", description="Tutup/archive thread ini (hanya bisa digunakan di dalam thread)")
async def close_thread(interaction: discord.Interaction):
    """Close/archive thread secara manual"""
    # Cek apakah command dipanggil di dalam thread
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ Command ini hanya bisa digunakan di dalam thread!", ephemeral=True)
        return
    
    thread = interaction.channel
    
    # Cek apakah thread sudah archived
    if thread.archived:
        await interaction.response.send_message("⚠️ Thread ini sudah di-archive!", ephemeral=True)
        return
    
    # Cek permission: user harus punya manage_messages atau moderator/admin
    if not _user_can_run_admin_actions(interaction.user):
        # Atau bisa juga cek apakah user adalah pembuat thread
        if thread.owner_id != interaction.user.id:
            await interaction.response.send_message("❌ Kamu tidak punya izin untuk menutup thread ini. Hanya moderator/admin atau pembuat thread yang bisa menutup.", ephemeral=True)
            return
    
    try:
        # Archive thread
        await thread.edit(archived=True, locked=False)
        # Remove dari auto-archive queue jika ada
        threads_to_archive.pop(thread.id, None)
        await interaction.response.send_message("✅ Thread berhasil ditutup (archived)!")
        print(f"[DEBUG] Thread {thread.name} di-archive oleh {interaction.user.name}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Bot tidak punya izin untuk menutup thread ini.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Failed to archive thread: {e}")
        await interaction.response.send_message(f"❌ Gagal menutup thread: {e}", ephemeral=True)

@bot.tree.command(name="scan_threads", description="Scan dan archive thread lama yang sudah lebih dari 15 menit (admin only)")
@app_commands.check(_metadao_admin_check)
async def scan_threads(interaction: discord.Interaction):
    """Manual trigger untuk scan dan archive thread lama"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        await scan_and_archive_old_threads()
        await interaction.followup.send("✅ Scan thread selesai! Cek console untuk detail.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Failed to scan threads: {e}")
        await interaction.followup.send(f"❌ Gagal scan thread: {e}", ephemeral=True)

@scan_threads.error
async def scan_threads_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menjalankan command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

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
                error_msg = str(e)
                # Check if it's a rate limit error
                if "rate limited" in error_msg.lower() or "429" in error_msg:
                    print(f"[ERROR] Rate limit error in on_message: {e}")
                    await message.channel.send(f"⚠️ **Rate Limited**: {error_msg}\n\nCoba lagi dalam beberapa saat.")
                else:
                    print(f"[ERROR] Unexpected error: {e}")
                    import traceback
                    traceback.print_exc()
                    await message.channel.send(f"❌ **Error**: {error_msg}")

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
            auto_archive_duration=60,  # Discord minimum (akan di-override oleh task 15 menit)
        )
        
        # Track thread untuk auto-archive setelah 15 menit
        threads_to_archive[thread.id] = time.time()
        print(f"[DEBUG] Thread {thread.id} ditambahkan ke auto-archive queue (15 menit)")

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
        error_msg = str(e)
        # Check if it's a rate limit error
        if "rate limited" in error_msg.lower() or "429" in error_msg:
            print(f"[ERROR] Rate limit error in !call: {e}")
            await ctx.send(f"⚠️ **Rate Limited**: {error_msg}\n\nCoba lagi dalam beberapa saat.")
        else:
            print(f"[ERROR] Unexpected error in !call: {e}")
            import traceback
            traceback.print_exc()
            await ctx.send(f"❌ **Error**: {error_msg}")

# --- RUN BOT ---
print("[DEBUG] Bot starting...")
bot.run(TOKEN)