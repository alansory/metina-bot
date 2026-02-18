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
from discord.ui import Button, View
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

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

# ============================================================================
# --- FITUR BARU: TOKEN LAUNCH TRACKER (DAMM V2) ---
# ============================================================================
# Monitor token yang akan launch dan detect ketika pool DAMM v2 tersedia
LAUNCH_TRACKER_ENABLED = os.getenv("LAUNCH_TRACKER_ENABLED", "true").lower() == "true"
LAUNCH_TRACKER_POLL_INTERVAL_SEC = int(os.getenv("LAUNCH_TRACKER_POLL_INTERVAL", "10"))  # Poll setiap 10 detik
LAUNCH_TRACKER_STATE_FILE = "launch_tracker_state.json"
LAUNCH_TRACKER_CHANNEL_ID = int(os.getenv("LAUNCH_TRACKER_CHANNEL_ID", str(DAMM_CHANNEL_ID))) if DAMM_CHANNEL_ID else None  # Default ke DAMM channel

# State untuk launch tracker
launch_tracker_tokens: Dict[str, Dict] = {}  # {token_address: {name, symbol, added_at, added_by, status}}
launch_detected_pools: Dict[str, str] = {}  # {token_address: pool_address} - untuk track yang sudah detect

# ============================================================================
# --- FITUR BARU: ICO TRACKER (DAILY + 1 HOUR REMINDER) ---
# ============================================================================
ICO_TRACKER_ENABLED = os.getenv("ICO_TRACKER_ENABLED", "true").lower() == "true"
ICO_TRACKER_STATE_FILE = "ico_tracker_state.json"
ICO_TRACKER_CHANNEL_ID = int(os.getenv("ICO_TRACKER_CHANNEL_ID", str(DAMM_CHANNEL_ID))) if DAMM_CHANNEL_ID else None

# State untuk ICO tracker
# Format: {ico_id: {name, token_symbol, end_time, target, committed, url, daily_notified_dates, hour_reminder_sent, ...}}
ico_tracker_list: Dict[str, Dict] = {}

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
ALLOWED_CHANNEL_ID = 1428299549507584080  # Channel LP Calls (lp-call) - untuk command !call
THREAD_SCAN_CHANNEL_ID = 1428996637237313546  # Channel LP Chat (lp-chat) - untuk scan & archive thread lama
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

# --- FITUR BARU: BOT CALL - MONITOR TOKEN BARU ---
BOT_CALL_CHANNEL_ID = int(os.getenv("BOT_CALL_CHANNEL_ID", "1443433566058053662")) or None  # Channel untuk notifikasi token baru
BOT_CALL_MIN_MARKET_CAP = float(os.getenv("BOT_CALL_MIN_MARKET_CAP", "250000"))  # Minimum market cap: 250k USD
BOT_CALL_MAX_MARKET_CAP = float(os.getenv("BOT_CALL_MAX_MARKET_CAP", "10000000"))  # Maximum market cap: 10jt USD
BOT_CALL_MIN_FEES_SOL = float(os.getenv("BOT_CALL_MIN_FEES_SOL", "10"))  # Minimum total fees: 20 SOL (bukan USD)
BOT_CALL_MIN_PRICE_CHANGE_1H = float(os.getenv("BOT_CALL_MIN_PRICE_CHANGE_1H", "35"))  # Minimum price change 1h: 35%
BOT_CALL_POLL_INTERVAL_MINUTES = int(os.getenv("BOT_CALL_POLL_INTERVAL", "5"))  # Poll setiap 2 menit
BOT_CALL_STATE_FILE = "bot_call_state.json"  # File untuk simpan state token yang sudah di-notifikasi
bot_call_notified_tokens: Dict[str, str] = {}  # {token_address: date_notified (YYYY-MM-DD)}
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "efd896ec-30ed-4c89-a990-32b315e13d20")  # Jupiter API key
USE_METEORA_FOR_FEES = os.getenv("USE_METEORA_FOR_FEES", "false").lower() == "true"  # Use Meteora for volume/fees data

# ============================================================================
# --- FITUR BARU: AUTO TRADING BOT (FULL AUTO) ---
# ============================================================================
# ⚠️ WARNING: Fitur ini SANGAT BERISIKO! Bisa kehilangan dana!
# Pastikan kamu paham risikonya sebelum mengaktifkan.
# ============================================================================

# Trading Wallet Config (PRIVATE KEY HARUS DARI ENV VARIABLE!)
TRADING_WALLET_PRIVATE_KEY = os.getenv("TRADING_WALLET_PRIVATE_KEY")  # Base58 private key
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
TRADING_CHANNEL_ID = int(os.getenv("TRADING_CHANNEL_ID", "0")) or None  # Channel untuk trading notifications

# Trading Parameters
TRADING_CONFIG = {
    # ⚠️ DRY RUN MODE - Set true untuk test tanpa pakai uang asli!
    "dry_run": os.getenv("TRADING_DRY_RUN", "true").lower() == "true",  # Default TRUE = TIDAK trade beneran
    
    "take_profit_percent": float(os.getenv("TRADING_TP_PERCENT", "7")),  # Take profit at 7%
    "stop_loss_percent": float(os.getenv("TRADING_SL_PERCENT", "5")),    # Stop loss at -5%
    "max_position_sol": float(os.getenv("TRADING_MAX_SOL", "0.5")),      # Max 0.5 SOL per trade
    "max_concurrent_positions": int(os.getenv("TRADING_MAX_POSITIONS", "3")),  # Max 3 positions
    "max_hold_minutes": int(os.getenv("TRADING_MAX_HOLD_MIN", "30")),    # Auto sell after 30 min
    "slippage_bps": int(os.getenv("TRADING_SLIPPAGE_BPS", "300")),       # 3% slippage (300 bps)
    "price_check_interval_sec": int(os.getenv("TRADING_CHECK_INTERVAL", "15")),  # Check price every 15s
    "auto_trade_from_bot_call": os.getenv("TRADING_AUTO_FROM_BOTCALL", "false").lower() == "true",
    "min_liquidity_usd": float(os.getenv("TRADING_MIN_LIQ", "5000")),    # Min $5000 liquidity
    "daily_loss_limit_sol": float(os.getenv("TRADING_DAILY_LOSS_LIMIT", "2")),  # Max 2 SOL loss per day
    
    # ============================================================================
    # HYPE TRADING CONFIG - Volume Spike + Social + KOL Detection
    # ============================================================================
    "hype_trading_enabled": os.getenv("HYPE_TRADING_ENABLED", "false").lower() == "true",
    "hype_scan_interval_sec": int(os.getenv("HYPE_SCAN_INTERVAL", "60")),  # Scan setiap 60 detik
    
    # Volume Spike Detection (1-5 menit)
    "min_volume_5m_usd": float(os.getenv("HYPE_MIN_VOL_5M", "50000")),     # Min $50k volume dalam 5 menit
    "min_volume_1h_usd": float(os.getenv("HYPE_MIN_VOL_1H", "100000")),    # Min $100k volume 1 jam
    "min_txns_5m": int(os.getenv("HYPE_MIN_TXNS_5M", "50")),               # Min 50 transaksi dalam 5 menit
    "min_buyers_5m": int(os.getenv("HYPE_MIN_BUYERS_5M", "30")),           # Min 30 unique buyers dalam 5 menit
    
    # Price Action Filter
    "min_price_change_5m": float(os.getenv("HYPE_MIN_PRICE_5M", "5")),     # Min +5% dalam 5 menit
    "max_price_change_5m": float(os.getenv("HYPE_MAX_PRICE_5M", "50")),    # Max +50% (hindari pump & dump)
    
    # Market Cap Filter
    "hype_min_mcap": float(os.getenv("HYPE_MIN_MCAP", "100000")),          # Min $100k market cap
    "hype_max_mcap": float(os.getenv("HYPE_MAX_MCAP", "5000000")),         # Max $5M market cap (early stage)
    
    # Social/Hype Score (dari DexScreener)
    "min_social_score": int(os.getenv("HYPE_MIN_SOCIAL", "0")),            # Min social score (0 = disabled)
    
    # KOL Tracking
    "kol_tracking_enabled": os.getenv("KOL_TRACKING_ENABLED", "false").lower() == "true",
    "min_kol_buys": int(os.getenv("HYPE_MIN_KOL_BUYS", "2")),              # Min 2 KOL beli dalam 5 menit
    "kol_buy_min_sol": float(os.getenv("KOL_BUY_MIN_SOL", "1")),           # Min 1 SOL pembelian KOL
    
    # Token Age Filter
    "max_token_age_hours": int(os.getenv("HYPE_MAX_AGE_HOURS", "72")),     # Max umur token 72 jam (3 hari)
    "min_token_age_minutes": int(os.getenv("HYPE_MIN_AGE_MIN", "5")),      # Min umur 5 menit (hindari honeypot)
}

# KOL (Key Opinion Leader) Wallets - tambahkan wallet KOL Solana yang dikenal
# Format: {"wallet": "address", "name": "KOL Name", "weight": 1-5}
KOL_WALLETS_FILE = "kol_wallets.json"
KOL_WALLETS: List[Dict[str, object]] = []

# Hype Detection State
HYPE_TOKENS_FILE = "hype_tokens_state.json"
hype_detected_tokens: Dict[str, Dict] = {}  # {token_address: detection_data}
hype_traded_tokens: Dict[str, str] = {}  # {token_address: date_traded}

def load_kol_wallets():
    """Load KOL wallet list from file."""
    global KOL_WALLETS
    try:
        if os.path.exists(KOL_WALLETS_FILE):
            with open(KOL_WALLETS_FILE, 'r') as f:
                KOL_WALLETS = json.load(f)
            print(f"[HYPE] Loaded {len(KOL_WALLETS)} KOL wallet(s)")
        else:
            # Default KOL wallets (some known Solana traders - ADD YOUR OWN!)
            KOL_WALLETS = [
                # Example format - replace dengan wallet KOL yang kamu track
                # {"wallet": "WALLET_ADDRESS", "name": "KOL Name", "weight": 3},
            ]
            save_kol_wallets()
            print(f"[HYPE] Created empty KOL wallets file - add wallets to {KOL_WALLETS_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to load KOL wallets: {e}")
        KOL_WALLETS = []

def save_kol_wallets():
    """Save KOL wallet list to file."""
    try:
        with open(KOL_WALLETS_FILE, 'w') as f:
            json.dump(KOL_WALLETS, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save KOL wallets: {e}")

def load_hype_state():
    """Load hype detection state."""
    global hype_detected_tokens, hype_traded_tokens
    try:
        if os.path.exists(HYPE_TOKENS_FILE):
            with open(HYPE_TOKENS_FILE, 'r') as f:
                data = json.load(f)
                hype_detected_tokens = data.get("detected", {})
                hype_traded_tokens = data.get("traded", {})
            print(f"[HYPE] Loaded state: {len(hype_detected_tokens)} detected, {len(hype_traded_tokens)} traded")
    except Exception as e:
        print(f"[ERROR] Failed to load hype state: {e}")

def save_hype_state():
    """Save hype detection state."""
    try:
        with open(HYPE_TOKENS_FILE, 'w') as f:
            json.dump({
                "detected": hype_detected_tokens,
                "traded": hype_traded_tokens
            }, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save hype state: {e}")

# --- LAUNCH TRACKER LOAD/SAVE ---
def load_launch_tracker_state():
    """Load launch tracker state from file."""
    global launch_tracker_tokens, launch_detected_pools
    try:
        if os.path.exists(LAUNCH_TRACKER_STATE_FILE):
            with open(LAUNCH_TRACKER_STATE_FILE, 'r') as f:
                data = json.load(f)
                launch_tracker_tokens = data.get("tokens", {})
                launch_detected_pools = data.get("detected_pools", {})
            print(f"[LAUNCH_TRACKER] Loaded {len(launch_tracker_tokens)} tracked token(s), {len(launch_detected_pools)} detected pool(s)")
    except Exception as e:
        print(f"[ERROR] Failed to load launch tracker state: {e}")
        launch_tracker_tokens = {}
        launch_detected_pools = {}

def save_launch_tracker_state():
    """Save launch tracker state to file."""
    try:
        with open(LAUNCH_TRACKER_STATE_FILE, 'w') as f:
            json.dump({
                "tokens": launch_tracker_tokens,
                "detected_pools": launch_detected_pools
            }, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save launch tracker state: {e}")

# --- ICO TRACKER LOAD/SAVE ---
def load_ico_tracker_state():
    """Load ICO tracker state from file."""
    global ico_tracker_list
    try:
        if os.path.exists(ICO_TRACKER_STATE_FILE):
            with open(ICO_TRACKER_STATE_FILE, 'r') as f:
                ico_tracker_list = json.load(f)
            print(f"[ICO_TRACKER] Loaded {len(ico_tracker_list)} tracked ICO(s)")
    except Exception as e:
        print(f"[ERROR] Failed to load ICO tracker state: {e}")
        ico_tracker_list = {}

def save_ico_tracker_state():
    """Save ICO tracker state to file."""
    try:
        with open(ICO_TRACKER_STATE_FILE, 'w') as f:
            json.dump(ico_tracker_list, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save ICO tracker state: {e}")

# Trading State
TRADING_POSITIONS_FILE = "trading_positions.json"
TRADING_HISTORY_FILE = "trading_history.json"
active_positions: Dict[str, Dict] = {}  # {token_address: position_data}
trading_history: List[Dict] = []  # History of closed trades
daily_pnl: float = 0.0  # Track daily P&L
daily_pnl_date: str = ""  # Date of daily P&L tracking

# Validate trading config on startup
if TRADING_ENABLED:
    if not TRADING_WALLET_PRIVATE_KEY:
        print("❌ TRADING_ENABLED=true but TRADING_WALLET_PRIVATE_KEY not set!")
        print("⚠️ Trading will be DISABLED for safety.")
        TRADING_ENABLED = False
    else:
        print(f"✅ Trading Bot ENABLED!")
        print(f"   - Take Profit: {TRADING_CONFIG['take_profit_percent']}%")
        print(f"   - Stop Loss: {TRADING_CONFIG['stop_loss_percent']}%")
        print(f"   - Max Position: {TRADING_CONFIG['max_position_sol']} SOL")
        print(f"   - Max Concurrent: {TRADING_CONFIG['max_concurrent_positions']} positions")
        print(f"   - Daily Loss Limit: {TRADING_CONFIG['daily_loss_limit_sol']} SOL")
else:
    print("ℹ️ Trading Bot DISABLED (set TRADING_ENABLED=true to enable)")

def load_trading_positions():
    """Load active trading positions from file."""
    global active_positions
    try:
        if os.path.exists(TRADING_POSITIONS_FILE):
            with open(TRADING_POSITIONS_FILE, 'r') as f:
                active_positions = json.load(f)
            print(f"[TRADING] Loaded {len(active_positions)} active position(s)")
    except Exception as e:
        print(f"[ERROR] Failed to load trading positions: {e}")
        active_positions = {}

def save_trading_positions():
    """Save active trading positions to file."""
    try:
        with open(TRADING_POSITIONS_FILE, 'w') as f:
            json.dump(active_positions, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save trading positions: {e}")

def load_trading_history():
    """Load trading history from file."""
    global trading_history
    try:
        if os.path.exists(TRADING_HISTORY_FILE):
            with open(TRADING_HISTORY_FILE, 'r') as f:
                trading_history = json.load(f)
            print(f"[TRADING] Loaded {len(trading_history)} historical trade(s)")
    except Exception as e:
        print(f"[ERROR] Failed to load trading history: {e}")
        trading_history = []

def save_trading_history():
    """Save trading history to file."""
    try:
        with open(TRADING_HISTORY_FILE, 'w') as f:
            json.dump(trading_history, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed to save trading history: {e}")

def reset_daily_pnl_if_needed():
    """Reset daily P&L if it's a new day."""
    global daily_pnl, daily_pnl_date
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_pnl_date != today:
        daily_pnl = 0.0
        daily_pnl_date = today
        print(f"[TRADING] Daily P&L reset for {today}")

async def get_token_price(token_address: str) -> Optional[float]:
    """Get current token price in USD from Jupiter/DexScreener."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        # Try Jupiter Price API first
        url = f"https://price.jup.ag/v6/price?ids={token_address}"
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                if "data" in data and token_address in data["data"]:
                    price = data["data"][token_address].get("price")
                    if price:
                        return float(price)
    except Exception as e:
        print(f"[TRADING] Jupiter price fetch failed: {e}")
    
    # Fallback to DexScreener
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get("pairs") or []
                if pairs:
                    # Get price from most liquid pair
                    best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    price = best_pair.get("priceUsd")
                    if price:
                        return float(price)
    except Exception as e:
        print(f"[TRADING] DexScreener price fetch failed: {e}")
    
    return None

async def fetch_token_safety(token_address: str) -> Optional[Dict]:
    """Fetch token safety data from deepnets.ai API."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        url = f"https://api.deepnets.ai/api/token-safety/{token_address}"
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                return data
            else:
                print(f"[TOKEN_SAFETY] API returned status {response.status}")
                return None
    except asyncio.TimeoutError:
        print(f"[TOKEN_SAFETY] Timeout fetching safety data for {token_address}")
        return None
    except Exception as e:
        print(f"[TOKEN_SAFETY] Error fetching safety data: {e}")
        return None

def create_token_safety_embeds(safety_data: Dict, token_address: str = None) -> List[discord.Embed]:
    """Create Discord embeds for token safety information."""
    embeds = []
    
    # Determine safety level color
    safety_level = safety_data.get("overallSafetyLevel", "UNKNOWN")
    if safety_level == "SAFE" or safety_level == "OK":
        color = 0x00ff00  # Green
    elif safety_level == "RISKY":
        color = 0xff9900  # Orange
    elif safety_level == "CRITICAL":
        color = 0xff0000  # Red
    else:
        color = 0x808080  # Gray
    
    # First embed: Token Properties
    properties_embed = discord.Embed(
        title="🛡️ Token Safety Analysis",
        color=color
    )
    
    # Token info
    token_name = safety_data.get("tokenName", "Unknown")
    token_symbol = safety_data.get("tokenSymbol", "Unknown")
    properties_embed.add_field(
        name="Token",
        value=f"**{token_name}** ({token_symbol})",
        inline=False
    )
    
    # Token properties
    is_mintable = safety_data.get("isMintable", False)
    is_freezable = safety_data.get("isFreezable", False)
    is_metadata_mutable = safety_data.get("isMetadataMutable", False)
    
    mintable_status = "✅ No" if not is_mintable else "❌ Yes"
    freezable_status = "✅ No" if not is_freezable else "❌ Yes"
    metadata_status = "✅ No" if not is_metadata_mutable else "❌ Yes"
    
    properties_embed.add_field(
        name="Mintable",
        value=mintable_status,
        inline=True
    )
    properties_embed.add_field(
        name="Freezable",
        value=freezable_status,
        inline=True
    )
    properties_embed.add_field(
        name="Metadata Mutable",
        value=metadata_status,
        inline=True
    )
    
    # Token properties analysis
    token_props_analysis = safety_data.get("tokenPropertiesAnalysis", "")
    if token_props_analysis:
        properties_embed.add_field(
            name="Properties Analysis",
            value=token_props_analysis,
            inline=False
        )
    
    embeds.append(properties_embed)
    
    # Second embed: RugCheck Score and Warnings
    score_embed = discord.Embed(
        title="📊 RugCheck Score",
        color=color
    )
    
    rugcheck_score = safety_data.get("rugcheckScore", 0)
    
    # Format safety level - simple and clean like the image
    safety_display = f"**{safety_level}** - Score: {rugcheck_score}/100"
    
    score_embed.add_field(
        name="⚠️ Overall Safety",
        value=safety_display,
        inline=False
    )
    
    # Warnings
    warnings = safety_data.get("warnings", [])
    critical_risks = safety_data.get("criticalRisks", [])
    
    if critical_risks:
        score_embed.add_field(
            name="⚠️ Critical Risks",
            value="\n".join([f"• {risk}" for risk in critical_risks]),
            inline=False
        )
    
    if warnings:
        warnings_text = "\n".join([f"• {warning}" for warning in warnings])
        score_embed.add_field(
            name="⚠️ Warnings",
            value=warnings_text,
            inline=False
        )
    
    # RugCheck risks
    rugcheck_risks = safety_data.get("rugCheckRisks", [])
    if rugcheck_risks:
        risks_text = "\n".join([f"• {risk}" for risk in rugcheck_risks])
        score_embed.add_field(
            name="RugCheck Risks",
            value=risks_text,
            inline=False
        )
    
    # Holder distribution info
    top_holder_ownership = safety_data.get("topHolderOwnership")
    top_ten_ownership = safety_data.get("topTenOwnership")
    top_network_ownership = safety_data.get("topNetworkOwnership")
    
    if top_holder_ownership or top_ten_ownership or top_network_ownership:
        holder_info = []
        if top_holder_ownership:
            holder_info.append(f"Top holder: {top_holder_ownership:.1f}%")
        if top_ten_ownership:
            holder_info.append(f"Top 10 holders: {top_ten_ownership:.1f}%")
        if top_network_ownership:
            holder_info.append(f"Top network: {top_network_ownership:.1f}%")
        
        score_embed.add_field(
            name="Holder Distribution",
            value="\n".join(holder_info),
            inline=False
        )
    
    # Liquidity analysis
    liquidity_analysis = safety_data.get("liquidityAnalysis", "")
    if liquidity_analysis:
        score_embed.add_field(
            name="Liquidity Analysis",
            value=liquidity_analysis,
            inline=False
        )
    
    # Add links to Deepnets.ai and GMGN if token_address is provided
    if token_address:
        links_value = (
            f"[🔍 Deepnets.ai](https://deepnets.ai/token/{token_address})\n"
            f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})"
        )
        score_embed.add_field(
            name="🔗 Links",
            value=links_value,
            inline=False
        )
    
    embeds.append(score_embed)
    
    return embeds

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300) -> Optional[Dict]:
    """Get swap quote from Jupiter API."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        
        async with http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                print(f"[TRADING] Jupiter quote error: {response.status} - {error_text}")
                return None
    except Exception as e:
        print(f"[TRADING] Jupiter quote failed: {e}")
        return None

# SOL mint address
SOL_MINT = "So11111111111111111111111111111111111111112"

async def check_jupiter_tradeable(token_address: str, test_amount_sol: float = 0.01) -> Dict:
    """
    Check if token is tradeable on Jupiter.
    Returns: {
        "tradeable": bool,
        "quote": quote_data or None,
        "out_amount": float (token amount),
        "price_impact": float (percentage),
        "routes": int (number of routes)
    }
    """
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    result = {
        "tradeable": False,
        "quote": None,
        "out_amount": 0,
        "price_impact": 0,
        "routes": 0,
        "error": None
    }
    
    try:
        # Convert SOL to lamports (1 SOL = 1e9 lamports)
        amount_lamports = int(test_amount_sol * 1_000_000_000)
        
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": SOL_MINT,
            "outputMint": token_address,
            "amount": str(amount_lamports),
            "slippageBps": 500,  # 5% slippage for checking
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        
        async with http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                quote = await response.json()
                
                # Check if we got a valid quote
                out_amount = quote.get("outAmount")
                if out_amount and int(out_amount) > 0:
                    result["tradeable"] = True
                    result["quote"] = quote
                    result["out_amount"] = int(out_amount)
                    
                    # Get price impact
                    price_impact = quote.get("priceImpactPct")
                    if price_impact:
                        try:
                            result["price_impact"] = float(price_impact) * 100  # Convert to percentage
                        except:
                            pass
                    
                    # Count routes
                    route_plan = quote.get("routePlan", [])
                    result["routes"] = len(route_plan)
                    
                    print(f"[JUPITER] ✅ Token tradeable! Out: {out_amount}, Impact: {result['price_impact']:.2f}%, Routes: {result['routes']}")
                else:
                    result["error"] = "No output amount"
                    print(f"[JUPITER] ❌ Token not tradeable - no output amount")
            else:
                error_text = await response.text()
                result["error"] = f"HTTP {response.status}"
                print(f"[JUPITER] ❌ Quote error: {response.status} - {error_text[:100]}")
                
    except asyncio.TimeoutError:
        result["error"] = "Timeout"
        print(f"[JUPITER] ❌ Quote timeout for {token_address[:8]}...")
    except Exception as e:
        result["error"] = str(e)
        print(f"[JUPITER] ❌ Quote failed: {e}")
    
    return result

async def execute_jupiter_swap(quote: Dict) -> Optional[str]:
    """Execute swap via Jupiter API. Returns transaction signature if successful."""
    global http_session
    
    if not TRADING_WALLET_PRIVATE_KEY:
        print("[TRADING] No private key configured!")
        return None
    
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        # Import solana libraries (lazy import to avoid startup errors if not installed)
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        from solders.signature import Signature
        import base58
        import base64
        
        # Decode private key
        try:
            private_key_bytes = base58.b58decode(TRADING_WALLET_PRIVATE_KEY)
            keypair = Keypair.from_bytes(private_key_bytes)
            wallet_pubkey = str(keypair.pubkey())
        except Exception as e:
            print(f"[TRADING] Invalid private key format: {e}")
            return None
        
        # Get swap transaction from Jupiter
        swap_url = "https://quote-api.jup.ag/v6/swap"
        swap_data = {
            "quoteResponse": quote,
            "userPublicKey": wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }
        
        async with http_session.post(swap_url, json=swap_data, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                error_text = await response.text()
                print(f"[TRADING] Jupiter swap request failed: {response.status} - {error_text}")
                return None
            
            swap_response = await response.json()
            swap_transaction = swap_response.get("swapTransaction")
            
            if not swap_transaction:
                print("[TRADING] No swap transaction in response")
                return None
        
        # Decode and sign transaction
        tx_bytes = base64.b64decode(swap_transaction)
        transaction = VersionedTransaction.from_bytes(tx_bytes)
        
        # Sign the transaction
        signed_tx = VersionedTransaction(transaction.message, [keypair])
        
        # Send transaction via Helius RPC
        rpc_url = HELIUS_RPC_URL if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
        
        tx_base64 = base64.b64encode(bytes(signed_tx)).decode('utf-8')
        
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                }
            ]
        }
        
        async with http_session.post(rpc_url, json=rpc_payload, timeout=aiohttp.ClientTimeout(total=60)) as response:
            result = await response.json()
            
            if "error" in result:
                print(f"[TRADING] Transaction failed: {result['error']}")
                return None
            
            signature = result.get("result")
            if signature:
                print(f"[TRADING] ✅ Transaction sent: {signature}")
                return signature
            
            return None
            
    except ImportError as e:
        print(f"[TRADING] Missing required library: {e}")
        print("[TRADING] Install with: pip install solders base58")
        return None
    except Exception as e:
        print(f"[TRADING] Swap execution error: {e}")
        import traceback
        traceback.print_exc()
        return None

async def open_trading_position(token_address: str, amount_sol: float, token_name: str = None, token_symbol: str = None) -> Tuple[bool, str]:
    """Open a new trading position (buy token with SOL)."""
    global active_positions, daily_pnl
    
    # Safety checks
    if not TRADING_ENABLED:
        return False, "Trading is disabled"
    
    if not TRADING_WALLET_PRIVATE_KEY:
        return False, "Trading wallet not configured"
    
    reset_daily_pnl_if_needed()
    
    # Check daily loss limit
    if daily_pnl <= -TRADING_CONFIG["daily_loss_limit_sol"]:
        return False, f"Daily loss limit reached ({daily_pnl:.4f} SOL)"
    
    # Check max concurrent positions
    if len(active_positions) >= TRADING_CONFIG["max_concurrent_positions"]:
        return False, f"Max concurrent positions ({TRADING_CONFIG['max_concurrent_positions']}) reached"
    
    # Check if already in position
    if token_address in active_positions:
        return False, "Already in position for this token"
    
    # Validate amount
    if amount_sol > TRADING_CONFIG["max_position_sol"]:
        amount_sol = TRADING_CONFIG["max_position_sol"]
        print(f"[TRADING] Amount capped to max: {amount_sol} SOL")
    
    if amount_sol < 0.01:
        return False, "Minimum amount is 0.01 SOL"
    
    # Get current token price before buying
    entry_price = await get_token_price(token_address)
    if not entry_price:
        return False, "Could not fetch token price"
    
    # Convert SOL to lamports
    amount_lamports = int(amount_sol * 1_000_000_000)
    
    # Get Jupiter quote (SOL -> Token)
    quote = await get_jupiter_quote(
        input_mint=SOL_MINT,
        output_mint=token_address,
        amount=amount_lamports,
        slippage_bps=TRADING_CONFIG["slippage_bps"]
    )
    
    if not quote:
        return False, "Could not get swap quote from Jupiter"
    
    # Check liquidity/output
    out_amount = int(quote.get("outAmount", 0))
    if out_amount <= 0:
        return False, "Invalid quote output amount"
    
    # Execute swap
    signature = await execute_jupiter_swap(quote)
    
    if not signature:
        return False, "Swap execution failed"
    
    # Record position
    now = time.time()
    position = {
        "token_address": token_address,
        "token_name": token_name or "Unknown",
        "token_symbol": token_symbol or "???",
        "entry_price_usd": entry_price,
        "entry_amount_sol": amount_sol,
        "entry_amount_lamports": amount_lamports,
        "tokens_received": out_amount,
        "entry_time": now,
        "entry_tx": signature,
        "take_profit_price": entry_price * (1 + TRADING_CONFIG["take_profit_percent"] / 100),
        "stop_loss_price": entry_price * (1 - TRADING_CONFIG["stop_loss_percent"] / 100),
        "max_hold_until": now + (TRADING_CONFIG["max_hold_minutes"] * 60),
        "status": "open",
    }
    
    active_positions[token_address] = position
    save_trading_positions()
    
    return True, signature

async def close_trading_position(token_address: str, reason: str = "manual") -> Tuple[bool, str, Optional[float]]:
    """Close a trading position (sell token for SOL). Returns (success, message, pnl_sol)."""
    global active_positions, daily_pnl, trading_history
    
    if token_address not in active_positions:
        return False, "Position not found", None
    
    position = active_positions[token_address]
    
    # Get current price
    current_price = await get_token_price(token_address)
    if not current_price:
        return False, "Could not fetch current price", None
    
    # Get Jupiter quote (Token -> SOL)
    quote = await get_jupiter_quote(
        input_mint=token_address,
        output_mint=SOL_MINT,
        amount=position["tokens_received"],
        slippage_bps=TRADING_CONFIG["slippage_bps"]
    )
    
    if not quote:
        return False, "Could not get sell quote from Jupiter", None
    
    # Execute swap
    signature = await execute_jupiter_swap(quote)
    
    if not signature:
        return False, "Sell execution failed", None
    
    # Calculate P&L
    out_amount_lamports = int(quote.get("outAmount", 0))
    out_amount_sol = out_amount_lamports / 1_000_000_000
    pnl_sol = out_amount_sol - position["entry_amount_sol"]
    pnl_percent = (pnl_sol / position["entry_amount_sol"]) * 100
    
    # Update daily P&L
    reset_daily_pnl_if_needed()
    daily_pnl += pnl_sol
    
    # Record to history
    history_entry = {
        **position,
        "exit_price_usd": current_price,
        "exit_amount_sol": out_amount_sol,
        "exit_time": time.time(),
        "exit_tx": signature,
        "pnl_sol": pnl_sol,
        "pnl_percent": pnl_percent,
        "close_reason": reason,
        "status": "closed",
    }
    trading_history.append(history_entry)
    save_trading_history()
    
    # Remove from active positions
    del active_positions[token_address]
    save_trading_positions()
    
    return True, signature, pnl_sol

async def send_trading_notification(title: str, description: str, color: int, position: Dict = None, pnl: float = None):
    """Send trading notification to Discord channel."""
    if not TRADING_CHANNEL_ID:
        return
    
    channel = bot.get_channel(TRADING_CHANNEL_ID)
    if not channel:
        # Fallback to bot call channel
        channel = bot.get_channel(BOT_CALL_CHANNEL_ID)
    
    if not channel:
        return
    
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    
    if position:
        token_symbol = position.get("token_symbol", "???")
        token_address = position.get("token_address", "")
        entry_price = position.get("entry_price_usd", 0)
        entry_sol = position.get("entry_amount_sol", 0)
        
        embed.add_field(name="Token", value=f"**{token_symbol}**\n`{token_address[:8]}...`", inline=True)
        embed.add_field(name="Entry", value=f"${entry_price:.8f}\n{entry_sol:.4f} SOL", inline=True)
        
        if pnl is not None:
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_percent = (pnl / entry_sol) * 100 if entry_sol > 0 else 0
            embed.add_field(name="P&L", value=f"{pnl_emoji} {pnl:+.4f} SOL\n({pnl_percent:+.2f}%)", inline=True)
        
        # Links
        links = f"[Solscan](https://solscan.io/token/{token_address}) | [Jupiter](https://jup.ag/swap/SOL-{token_address})"
        embed.add_field(name="Links", value=links, inline=False)
    
    embed.set_footer(text=f"Daily P&L: {daily_pnl:+.4f} SOL | Positions: {len(active_positions)}/{TRADING_CONFIG['max_concurrent_positions']}")
    
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[TRADING] Failed to send notification: {e}")

# ============================================================================
# --- HYPE TRADING: Volume Spike + Social + KOL Detection ---
# ============================================================================

async def fetch_trending_tokens_dexscreener() -> List[Dict]:
    """Fetch trending/boosted tokens dari DexScreener dengan data real-time."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    trending_tokens = []
    
    try:
        # DexScreener Boosted Tokens API (tokens yang di-boost/promoted)
        boosted_url = "https://api.dexscreener.com/token-boosts/latest/v1"
        async with http_session.get(boosted_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                # Filter hanya Solana tokens
                for token in data[:50]:  # Limit 50
                    if token.get("chainId") == "solana":
                        trending_tokens.append({
                            "address": token.get("tokenAddress"),
                            "source": "dexscreener_boost",
                            "boost_amount": token.get("amount", 0),
                        })
    except Exception as e:
        print(f"[HYPE] Error fetching DexScreener boosted: {e}")
    
    try:
        # DexScreener Top Tokens by Volume (Solana)
        top_url = "https://api.dexscreener.com/latest/dex/tokens/solana"
        # Note: This endpoint might not exist, will fallback to search
    except Exception as e:
        print(f"[HYPE] Error fetching DexScreener top: {e}")
    
    return trending_tokens

async def get_token_hype_data(token_address: str) -> Optional[Dict]:
    """Get detailed token data including volume, txns, social dari DexScreener."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                return None
            
            data = await response.json()
            pairs = data.get("pairs") or []
            
            if not pairs:
                return None
            
            # Aggregate data from all pairs (pilih pair Solana dengan liquidity terbesar)
            solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not solana_pairs:
                return None
            
            # Sort by liquidity
            solana_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            best_pair = solana_pairs[0]
            
            # Get base token info
            base_token = best_pair.get("baseToken", {})
            
            # Extract time-based metrics
            volume_5m = float(best_pair.get("volume", {}).get("m5", 0) or 0)
            volume_1h = float(best_pair.get("volume", {}).get("h1", 0) or 0)
            volume_24h = float(best_pair.get("volume", {}).get("h24", 0) or 0)
            
            txns_5m = best_pair.get("txns", {}).get("m5", {})
            txns_1h = best_pair.get("txns", {}).get("h1", {})
            
            buys_5m = int(txns_5m.get("buys", 0) or 0)
            sells_5m = int(txns_5m.get("sells", 0) or 0)
            total_txns_5m = buys_5m + sells_5m
            
            buys_1h = int(txns_1h.get("buys", 0) or 0)
            sells_1h = int(txns_1h.get("sells", 0) or 0)
            
            # Price changes
            price_change_5m = float(best_pair.get("priceChange", {}).get("m5", 0) or 0)
            price_change_1h = float(best_pair.get("priceChange", {}).get("h1", 0) or 0)
            price_change_24h = float(best_pair.get("priceChange", {}).get("h24", 0) or 0)
            
            # Market cap & liquidity
            market_cap = float(best_pair.get("fdv", 0) or best_pair.get("marketCap", 0) or 0)
            liquidity_usd = float(best_pair.get("liquidity", {}).get("usd", 0) or 0)
            
            # Token age (dari pairCreatedAt)
            pair_created_at = best_pair.get("pairCreatedAt")
            token_age_hours = None
            if pair_created_at:
                try:
                    created_ts = int(pair_created_at) / 1000  # Convert ms to seconds
                    token_age_hours = (time.time() - created_ts) / 3600
                except:
                    pass
            
            # Social info (if available)
            info = best_pair.get("info", {})
            socials = info.get("socials", [])
            has_twitter = any(s.get("type") == "twitter" for s in socials)
            has_telegram = any(s.get("type") == "telegram" for s in socials)
            has_website = bool(info.get("websites"))
            
            # Calculate hype score
            hype_score = 0
            
            # Volume score (0-30 points)
            if volume_5m >= 100000:
                hype_score += 30
            elif volume_5m >= 50000:
                hype_score += 20
            elif volume_5m >= 25000:
                hype_score += 10
            
            # Transaction count score (0-20 points)
            if total_txns_5m >= 100:
                hype_score += 20
            elif total_txns_5m >= 50:
                hype_score += 15
            elif total_txns_5m >= 25:
                hype_score += 10
            
            # Buy pressure score (0-20 points)
            if total_txns_5m > 0:
                buy_ratio = buys_5m / total_txns_5m
                if buy_ratio >= 0.7:
                    hype_score += 20
                elif buy_ratio >= 0.6:
                    hype_score += 15
                elif buy_ratio >= 0.5:
                    hype_score += 10
            
            # Price momentum score (0-15 points)
            if 5 <= price_change_5m <= 30:
                hype_score += 15
            elif 0 < price_change_5m < 5:
                hype_score += 5
            
            # Social presence score (0-15 points)
            if has_twitter:
                hype_score += 10
            if has_telegram:
                hype_score += 3
            if has_website:
                hype_score += 2
            
            return {
                "address": token_address,
                "name": base_token.get("name", "Unknown"),
                "symbol": base_token.get("symbol", "???"),
                "price_usd": float(best_pair.get("priceUsd", 0) or 0),
                "market_cap": market_cap,
                "liquidity_usd": liquidity_usd,
                
                # Volume metrics
                "volume_5m": volume_5m,
                "volume_1h": volume_1h,
                "volume_24h": volume_24h,
                
                # Transaction metrics
                "txns_5m": total_txns_5m,
                "buys_5m": buys_5m,
                "sells_5m": sells_5m,
                "buys_1h": buys_1h,
                "sells_1h": sells_1h,
                "buy_ratio_5m": buys_5m / total_txns_5m if total_txns_5m > 0 else 0,
                
                # Price changes
                "price_change_5m": price_change_5m,
                "price_change_1h": price_change_1h,
                "price_change_24h": price_change_24h,
                
                # Token info
                "token_age_hours": token_age_hours,
                "has_twitter": has_twitter,
                "has_telegram": has_telegram,
                "has_website": has_website,
                
                # Calculated scores
                "hype_score": hype_score,
                
                # Pair info
                "pair_address": best_pair.get("pairAddress"),
                "dex_id": best_pair.get("dexId"),
            }
            
    except Exception as e:
        print(f"[HYPE] Error fetching token data for {token_address[:8]}...: {e}")
        return None

async def check_kol_buys(token_address: str, time_window_minutes: int = 5) -> List[Dict]:
    """Check if any KOL wallets bought this token recently."""
    if not TRADING_CONFIG.get("kol_tracking_enabled") or not KOL_WALLETS:
        return []
    
    kol_buys = []
    min_sol = TRADING_CONFIG.get("kol_buy_min_sol", 1)
    
    # This would require checking recent transactions for each KOL wallet
    # For efficiency, we can use Helius to batch check
    # For now, return empty - this is a placeholder for advanced implementation
    
    # TODO: Implement KOL buy checking via Helius API
    # 1. For each KOL wallet, fetch recent SWAP transactions
    # 2. Check if any swap involves the target token
    # 3. Return list of KOL buys with details
    
    return kol_buys

def token_meets_hype_criteria(hype_data: Dict) -> Tuple[bool, List[str]]:
    """Check if token meets all hype trading criteria. Returns (passed, reasons)."""
    reasons = []
    
    cfg = TRADING_CONFIG
    
    # Volume check (5 menit)
    volume_5m = hype_data.get("volume_5m", 0)
    min_vol_5m = cfg.get("min_volume_5m_usd", 50000)
    if volume_5m < min_vol_5m:
        reasons.append(f"Volume 5m ${volume_5m:,.0f} < ${min_vol_5m:,.0f}")
    
    # Transaction count check
    txns_5m = hype_data.get("txns_5m", 0)
    min_txns = cfg.get("min_txns_5m", 50)
    if txns_5m < min_txns:
        reasons.append(f"Txns 5m {txns_5m} < {min_txns}")
    
    # Buyers check
    buys_5m = hype_data.get("buys_5m", 0)
    min_buyers = cfg.get("min_buyers_5m", 30)
    if buys_5m < min_buyers:
        reasons.append(f"Buyers 5m {buys_5m} < {min_buyers}")
    
    # Price change check
    price_change_5m = hype_data.get("price_change_5m", 0)
    min_price = cfg.get("min_price_change_5m", 5)
    max_price = cfg.get("max_price_change_5m", 50)
    if price_change_5m < min_price:
        reasons.append(f"Price change 5m {price_change_5m:.1f}% < {min_price}%")
    if price_change_5m > max_price:
        reasons.append(f"Price change 5m {price_change_5m:.1f}% > {max_price}% (pump risk)")
    
    # Market cap check
    market_cap = hype_data.get("market_cap", 0)
    min_mcap = cfg.get("hype_min_mcap", 100000)
    max_mcap = cfg.get("hype_max_mcap", 5000000)
    if market_cap < min_mcap:
        reasons.append(f"MCap ${market_cap:,.0f} < ${min_mcap:,.0f}")
    if market_cap > max_mcap:
        reasons.append(f"MCap ${market_cap:,.0f} > ${max_mcap:,.0f}")
    
    # Liquidity check
    liquidity = hype_data.get("liquidity_usd", 0)
    min_liq = cfg.get("min_liquidity_usd", 5000)
    if liquidity < min_liq:
        reasons.append(f"Liquidity ${liquidity:,.0f} < ${min_liq:,.0f}")
    
    # Token age check
    token_age = hype_data.get("token_age_hours")
    if token_age is not None:
        max_age = cfg.get("max_token_age_hours", 72)
        min_age_min = cfg.get("min_token_age_minutes", 5)
        if token_age > max_age:
            reasons.append(f"Token age {token_age:.1f}h > {max_age}h")
        if token_age * 60 < min_age_min:
            reasons.append(f"Token too new ({token_age*60:.1f}min < {min_age_min}min)")
    
    # Buy pressure check (optional but helpful)
    buy_ratio = hype_data.get("buy_ratio_5m", 0)
    if buy_ratio < 0.5:
        reasons.append(f"Low buy pressure ({buy_ratio:.0%} buyers)")
    
    passed = len(reasons) == 0
    return passed, reasons

async def scan_for_hype_tokens() -> List[Dict]:
    """Scan for tokens that meet hype criteria. Optimized for speed."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    qualifying_tokens = []
    scanned_count = 0
    
    try:
        print("[HYPE] 🔍 Starting token scan...")
        
        # Method 1: Scan DexScreener boosted tokens
        boosted = await fetch_trending_tokens_dexscreener()
        print(f"[HYPE] Got {len(boosted)} from boosted/trending")
        
        # Method 2: Get token profiles with recent activity
        profiles_url = "https://api.dexscreener.com/token-profiles/latest/v1"
        try:
            async with http_session.get(profiles_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    profiles = await response.json()
                    existing_addrs = {t.get("address") for t in boosted}
                    for profile in profiles[:20]:  # Reduced limit
                        if profile.get("chainId") == "solana":
                            token_addr = profile.get("tokenAddress")
                            if token_addr and token_addr not in existing_addrs:
                                boosted.append({
                                    "address": token_addr,
                                    "source": "dexscreener_profile"
                                })
                    print(f"[HYPE] Added {len([p for p in profiles[:20] if p.get('chainId') == 'solana'])} from profiles")
        except asyncio.TimeoutError:
            print("[HYPE] ⚠️ Profiles API timeout, continuing...")
        except Exception as e:
            print(f"[HYPE] ⚠️ Profiles fetch error: {e}")
        
        total_to_scan = min(len(boosted), 15)  # Limit to 15 for faster response
        print(f"[HYPE] Scanning {total_to_scan} tokens...")
        
        # Check each token
        today = datetime.now().strftime("%Y-%m-%d")
        
        for token_info in boosted[:total_to_scan]:
            token_address = token_info.get("address")
            if not token_address:
                continue
            
            # Skip if already traded today
            if hype_traded_tokens.get(token_address) == today:
                continue
            
            # Skip if already in position
            if token_address in active_positions:
                continue
            
            scanned_count += 1
            
            # Get detailed hype data with timeout
            try:
                hype_data = await asyncio.wait_for(
                    get_token_hype_data(token_address),
                    timeout=8.0  # 8 second timeout per token
                )
            except asyncio.TimeoutError:
                print(f"[HYPE] ⚠️ Timeout fetching {token_address[:8]}...")
                continue
            except Exception as e:
                print(f"[HYPE] ⚠️ Error fetching {token_address[:8]}...: {e}")
                continue
            
            if not hype_data:
                continue
            
            # Check criteria
            passed, reasons = token_meets_hype_criteria(hype_data)
            
            symbol = hype_data.get("symbol", "???")
            
            if passed:
                print(f"[HYPE] ✅ {symbol} QUALIFIES! Score: {hype_data.get('hype_score', 0)}")
                print(f"       CA: {token_address}")
                print(f"       🔗 https://gmgn.ai/sol/token/{token_address}")
                qualifying_tokens.append(hype_data)
            else:
                vol_5m = hype_data.get("volume_5m", 0)
                if vol_5m >= 10000:  # Only log tokens with some activity
                    print(f"[HYPE] ❌ {symbol} (vol=${vol_5m:,.0f}): {reasons[0] if reasons else 'unknown'}")
                    print(f"       CA: {token_address} | https://gmgn.ai/sol/token/{token_address}")
            
            # Minimal delay
            await asyncio.sleep(0.2)
        
        # Sort by hype score
        qualifying_tokens.sort(key=lambda x: x.get("hype_score", 0), reverse=True)
        
        print(f"[HYPE] ✅ Scan complete: {scanned_count} scanned, {len(qualifying_tokens)} qualified")
        
    except Exception as e:
        print(f"[HYPE] ❌ Error in scan: {e}")
        import traceback
        traceback.print_exc()
    
    return qualifying_tokens

async def execute_hype_trade(hype_data: Dict) -> Tuple[bool, str]:
    """Execute trade for a qualifying hype token. Supports DRY RUN mode."""
    token_address = hype_data.get("address")
    token_name = hype_data.get("name", "Unknown")
    token_symbol = hype_data.get("symbol", "???")
    
    # Check DRY RUN mode
    is_dry_run = TRADING_CONFIG.get("dry_run", True)
    
    if is_dry_run:
        # DRY RUN: Simulate trade without actually executing
        print(f"[DRY RUN] 🧪 Would trade {token_symbol} with {TRADING_CONFIG.get('max_position_sol', 0.5)} SOL")
        
        # Mark as "traded" for today (to avoid repeated notifications)
        today = datetime.now().strftime("%Y-%m-%d")
        hype_traded_tokens[token_address] = today
        save_hype_state()
        
        # Store detection data for reference
        hype_detected_tokens[token_address] = {
            **hype_data,
            "detected_at": time.time(),
            "traded": False,  # Not actually traded
            "dry_run": True
        }
        save_hype_state()
        
        return True, "DRY_RUN_SIMULATED"
    
    # REAL TRADE: Open position
    amount_sol = TRADING_CONFIG.get("max_position_sol", 0.5)
    
    success, result = await open_trading_position(
        token_address=token_address,
        amount_sol=amount_sol,
        token_name=token_name,
        token_symbol=token_symbol
    )
    
    if success:
        # Mark as traded today
        today = datetime.now().strftime("%Y-%m-%d")
        hype_traded_tokens[token_address] = today
        save_hype_state()
        
        # Store detection data for reference
        hype_detected_tokens[token_address] = {
            **hype_data,
            "detected_at": time.time(),
            "traded": True,
            "dry_run": False
        }
        save_hype_state()
    
    return success, result

async def send_hype_notification(hype_data: Dict, trade_result: str = None, is_dry_run: bool = False):
    """Send notification about hype token detection/trade."""
    channel = bot.get_channel(TRADING_CHANNEL_ID) or bot.get_channel(BOT_CALL_CHANNEL_ID)
    if not channel:
        return
    
    token_address = hype_data.get("address", "")
    symbol = hype_data.get("symbol", "???")
    name = hype_data.get("name", "Unknown")
    
    # Check dry run mode
    is_dry_run = is_dry_run or TRADING_CONFIG.get("dry_run", True) or trade_result == "DRY_RUN_SIMULATED"
    
    if trade_result and not is_dry_run:
        title = f"🔥 HYPE TRADE: {symbol}"
        color = 0xff6b00  # Orange
        desc = f"**{name}** (`{symbol}`)\n\nAuto-traded based on hype signals!"
    elif trade_result and is_dry_run:
        title = f"🧪 [DRY RUN] HYPE DETECTED: {symbol}"
        color = 0x9b59b6  # Purple for dry run
        desc = f"**{name}** (`{symbol}`)\n\n⚠️ **DRY RUN MODE** - Trade TIDAK dieksekusi!\nIni hanya simulasi untuk testing."
    else:
        title = f"🔥 HYPE DETECTED: {symbol}"
        color = 0xffaa00  # Yellow
        desc = f"**{name}** (`{symbol}`)\n\nToken showing strong hype signals!"
    
    embed = discord.Embed(
        title=title,
        description=desc,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    
    # Volume & Activity
    embed.add_field(
        name="📊 Volume (5min)",
        value=f"${hype_data.get('volume_5m', 0):,.0f}",
        inline=True
    )
    embed.add_field(
        name="💹 Txns (5min)",
        value=f"{hype_data.get('txns_5m', 0)} ({hype_data.get('buys_5m', 0)} buys)",
        inline=True
    )
    embed.add_field(
        name="📈 Price (5min)",
        value=f"{hype_data.get('price_change_5m', 0):+.1f}%",
        inline=True
    )
    
    # Market data
    embed.add_field(
        name="💰 Market Cap",
        value=_format_usd(hype_data.get("market_cap")),
        inline=True
    )
    embed.add_field(
        name="💧 Liquidity",
        value=_format_usd(hype_data.get("liquidity_usd")),
        inline=True
    )
    embed.add_field(
        name="🎯 Hype Score",
        value=f"{hype_data.get('hype_score', 0)}/100",
        inline=True
    )
    
    # Social indicators
    social_parts = []
    if hype_data.get("has_twitter"):
        social_parts.append("✅ Twitter")
    if hype_data.get("has_telegram"):
        social_parts.append("✅ Telegram")
    if hype_data.get("has_website"):
        social_parts.append("✅ Website")
    social_str = " | ".join(social_parts) if social_parts else "❌ No socials"
    embed.add_field(name="🌐 Socials", value=social_str, inline=False)
    
    # Links
    links = (
        f"[DexScreener](https://dexscreener.com/solana/{token_address}) | "
        f"[Jupiter](https://jup.ag/swap/SOL-{token_address}) | "
        f"[GMGN](https://gmgn.ai/sol/token/{token_address})"
    )
    embed.add_field(name="🔗 Links", value=links, inline=False)
    
    if trade_result:
        embed.add_field(name="📝 Tx", value=f"`{trade_result[:20]}...`", inline=False)
    
    embed.set_footer(text=f"Token: {token_address[:12]}...")
    
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[HYPE] Failed to send notification: {e}")

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

def load_bot_call_state():
    """Load persisted bot call notification state and cleanup old dates."""
    global bot_call_notified_tokens
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        
        if os.path.exists(BOT_CALL_STATE_FILE):
            with open(BOT_CALL_STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # Migrate old timestamp format to date format if needed
                    cleaned_data = {}
                    for addr, value in data.items():
                        if isinstance(value, (int, float)):
                            # Old format: timestamp, convert to date
                            ts = value if value > 1e10 else value * 1000
                            date = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts).strftime("%Y-%m-%d")
                            if date == today:
                                cleaned_data[addr] = date
                        elif isinstance(value, str):
                            # New format: date string
                            if value == today:
                                cleaned_data[addr] = value
                    
                    bot_call_notified_tokens = cleaned_data
                    print(f"[DEBUG] Loaded bot call state for {len(bot_call_notified_tokens)} token(s) (today: {today})")
                    
                    # Save cleaned data if we removed old entries
                    if len(cleaned_data) != len(data):
                        save_bot_call_state()
                else:
                    bot_call_notified_tokens = {}
        else:
            bot_call_notified_tokens = {}
    except Exception as e:
        print(f"[ERROR] Failed to load bot call state: {e}")
        bot_call_notified_tokens = {}

def save_bot_call_state():
    """Persist bot call notification state to disk."""
    try:
        with open(BOT_CALL_STATE_FILE, "w") as f:
            json.dump(bot_call_notified_tokens, f, indent=4)
        print("[DEBUG] Saved bot call state")
    except Exception as e:
        print(f"[ERROR] Failed to save bot call state: {e}")

# Load data on startup
load_tracked_wallets()
load_default_wallets()
load_metadao_state()
load_bot_call_state()

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
    """Extract MetaDAO launch data blob from rendered HTML.
    Supports multiple parsing methods for different page structures.
    """
    items = []
    
    # Method 1: Try Next.js __NEXT_DATA__ format
    next_data_marker = '<script id="__NEXT_DATA__" type="application/json">'
    next_start = html.find(next_data_marker)
    if next_start != -1:
        next_start += len(next_data_marker)
        next_end = html.find('</script>', next_start)
        if next_end != -1:
            try:
                next_data = json.loads(html[next_start:next_end])
                # Try different paths where projects data might be
                props = next_data.get("props", {}).get("pageProps", {})
                
                # Check common data locations
                for key in ["projects", "items", "launches", "raises", "fundraises", "data"]:
                    if key in props:
                        data = props[key]
                        if isinstance(data, list):
                            items.extend(data)
                            print(f"[DEBUG] MetaDAO: Found {len(data)} items via Next.js __NEXT_DATA__ ({key})")
                        elif isinstance(data, dict) and "items" in data:
                            items.extend(data["items"])
                            print(f"[DEBUG] MetaDAO: Found {len(data['items'])} items via Next.js ({key}.items)")
                
                # Also check dehydratedState for React Query
                dehydrated = props.get("dehydratedState", {}).get("queries", [])
                for query in dehydrated:
                    state = query.get("state", {}).get("data", {})
                    if isinstance(state, dict):
                        for key in ["items", "projects", "launches"]:
                            if key in state and isinstance(state[key], list):
                                items.extend(state[key])
                                print(f"[DEBUG] MetaDAO: Found {len(state[key])} items via dehydratedState")
                    elif isinstance(state, list):
                        items.extend(state)
                        print(f"[DEBUG] MetaDAO: Found {len(state)} items via dehydratedState (list)")
                
                if items:
                    return items
            except json.JSONDecodeError as e:
                print(f"[DEBUG] MetaDAO: Failed to parse __NEXT_DATA__: {e}")
    
    # Method 2: Try direct JSON marker {"items":[
    marker = '{"items":['
    start = html.find(marker)
    if start != -1:
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
        if end is not None:
            payload = html[start:end]
            try:
                data = json.loads(payload)
                result = data.get("items", [])
                if isinstance(result, list) and result:
                    print(f"[DEBUG] MetaDAO: Found {len(result)} items via direct marker")
                    return result
            except json.JSONDecodeError as e:
                print(f"[DEBUG] MetaDAO: Failed to decode direct payload: {e}")
    
    # Method 3: Try to find any JSON array with project-like data
    # Look for patterns like "fundraise" or "timeRemaining" in JSON
    for pattern in ['"fundraise"', '"timeRemaining"', '"organizationSlug"', '"minimumRaise"']:
        idx = html.find(pattern)
        if idx != -1:
            # Try to find the enclosing array/object
            # Look backwards for array start
            array_start = html.rfind('[', max(0, idx - 5000), idx)
            if array_start != -1:
                # Find matching end
                depth = 0
                end = None
                for i in range(array_start, min(len(html), array_start + 50000)):
                    if html[i] == '[':
                        depth += 1
                    elif html[i] == ']':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end:
                    try:
                        data = json.loads(html[array_start:end])
                        if isinstance(data, list) and len(data) > 0:
                            # Check if it looks like project data
                            first = data[0]
                            if isinstance(first, dict) and any(k in first for k in ["name", "id", "timeRemaining", "organizationSlug"]):
                                print(f"[DEBUG] MetaDAO: Found {len(data)} items via pattern search")
                                return data
                    except json.JSONDecodeError:
                        pass
            break  # Only try the first pattern found
    
    print(f"[DEBUG] MetaDAO: No items found in HTML (length: {len(html)})")
    return items

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

async def fetch_metadao_launches(max_retries: int = 3) -> List[Dict[str, object]]:
    """Fetch active MetaDAO launches with remaining time."""
    global http_session
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    # Headers untuk menghindari rate limiting (seperti browser biasa)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    
    html = None
    
    # Retry logic dengan exponential backoff
    for attempt in range(max_retries):
        try:
            async with http_session.get(
                METADAO_PROJECTS_URL, 
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                
                # Handle rate limiting (429)
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    wait_time = min(retry_after, 120) * (attempt + 1)  # Exponential backoff
                    print(f"[METADAO] Rate limited (429) - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                    await asyncio.sleep(wait_time)
                    continue
                
                response.raise_for_status()
                html = await response.text()
                break  # Success, exit retry loop
                
        except aiohttp.ClientResponseError as e:
            if e.status == 429 and attempt < max_retries - 1:
                wait_time = 30 * (attempt + 1)
                print(f"[METADAO] Rate limited - waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                await asyncio.sleep(wait_time)
                continue
            print(f"[ERROR] Failed to fetch MetaDAO projects (HTTP {e.status}): {e}")
            return []
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                wait_time = 10 * (attempt + 1)
                print(f"[METADAO] Timeout - retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            print(f"[ERROR] MetaDAO request timeout after {max_retries} attempts")
            return []
        except Exception as e:
            print(f"[ERROR] Failed to fetch MetaDAO projects: {e}")
            return []
    
    if not html:
        print(f"[ERROR] Failed to fetch MetaDAO HTML after {max_retries} attempts")
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
        timestamp=datetime.now(timezone.utc)
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

# --- HELPER: FETCH VOLUME/FEES FROM METEORA ---
def fetch_meteora_volume_and_fees(token_address: str) -> Tuple[Optional[float], Optional[float]]:
    """Fetch volume and fees data from Meteora pools for a token address.
    Returns: (volume_24h_usd, fees_24h_usd)"""
    if not USE_METEORA_FOR_FEES:
        return None, None
    
    try:
        # Meteora API endpoint untuk pools
        url = 'https://dlmm-api.meteora.ag/pair/all_by_groups'
        params = {
            'search_term': token_address,
            'sort_key': 'tvl',
            'order_by': 'desc',
            'limit': 20  # Ambil lebih banyak pools untuk akumulasi
        }
        
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return None, None
        
        data = response.json()
        total_volume_24h = 0
        total_fees_24h = 0
        
        # Extract volume and fees from pools
        if isinstance(data, dict) and 'groups' in data:
            for group in data.get('groups', []):
                pools = group.get('pairs', [])
                for pool in pools:
                    try:
                        # Meteora API provides volume and fees data
                        volume_24h = (
                            pool.get('trade_volume_24h') or 
                            (pool.get('volume', {}).get('hour_24') if isinstance(pool.get('volume'), dict) else None) or
                            pool.get('volume24h') or
                            pool.get('volume_24h')
                        )
                        
                        fees_24h = (
                            pool.get('fees_24h') or
                            (pool.get('fees', {}).get('hour_24') if isinstance(pool.get('fees'), dict) else None) or
                            pool.get('fees24h') or
                            pool.get('fees_24h')
                        )
                        
                        if volume_24h:
                            try:
                                total_volume_24h += float(volume_24h)
                            except (ValueError, TypeError):
                                pass
                        
                        if fees_24h:
                            try:
                                total_fees_24h += float(fees_24h)
                            except (ValueError, TypeError):
                                pass
                    except Exception:
                        continue
        
        volume = total_volume_24h if total_volume_24h > 0 else None
        fees = total_fees_24h if total_fees_24h > 0 else None
        
        return volume, fees
    except Exception as e:
        print(f"[DEBUG] Error fetching Meteora data for {token_address[:8]}...: {e}")
        return None, None

# --- HELPER: FETCH SOL PRICE ---
async def fetch_sol_price() -> float:
    """Fetch SOL price in USD. Try multiple sources: CoinGecko, Jupiter, then default."""
    global http_session
    default_price = 125.0
    
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    # Try CoinGecko first (more reliable)
    try:
        print(f"[DEBUG] Fetching SOL price from CoinGecko...")
        async with http_session.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                data = await response.json()
                if "solana" in data and "usd" in data["solana"]:
                    price = float(data["solana"]["usd"])
                    print(f"[INFO] SOL Price from CoinGecko: ${price:.2f}")
                    return price
                else:
                    print(f"[DEBUG] CoinGecko response missing solana/usd data: {data}")
            else:
                print(f"[DEBUG] CoinGecko returned status {response.status}")
    except aiohttp.ClientError as e:
        print(f"[DEBUG] CoinGecko connection error: {type(e).__name__}: {e}")
    except asyncio.TimeoutError:
        print(f"[DEBUG] CoinGecko request timeout")
    except Exception as e:
        print(f"[DEBUG] CoinGecko price fetch failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    
    # Try Jupiter as fallback
    try:
        print(f"[DEBUG] Fetching SOL price from Jupiter (fallback)...")
        price_url = "https://price.jup.ag/v4/price?ids=SOL"
        async with http_session.get(price_url, timeout=aiohttp.ClientTimeout(total=10)) as price_resp:
            if price_resp.status == 200:
                price_data = await price_resp.json()
                if "data" in price_data and "SOL" in price_data["data"]:
                    price = float(price_data["data"]["SOL"].get("price", default_price))
                    print(f"[INFO] SOL Price from Jupiter: ${price:.2f}")
                    return price
                else:
                    print(f"[DEBUG] Jupiter response missing data/SOL: {price_data}")
            else:
                print(f"[DEBUG] Jupiter returned status {price_resp.status}")
    except aiohttp.ClientError as e:
        print(f"[DEBUG] Jupiter connection error: {type(e).__name__}: {e}")
    except asyncio.TimeoutError:
        print(f"[DEBUG] Jupiter request timeout")
    except Exception as e:
        print(f"[DEBUG] Jupiter price fetch failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"[WARN] Could not fetch SOL price from any source, using default ${default_price}")
    return default_price

# --- HELPER: FETCH NEW TOKENS FROM JUPITER API ---
async def fetch_new_tokens() -> List[Dict[str, object]]:
    """Fetch new tokens from Jupiter API that meet criteria."""
    global http_session
    
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    try:
        url = f"https://api.jup.ag/tokens/v2/toptraded/1h?limit=100&minMcap={int(BOT_CALL_MIN_MARKET_CAP)}&maxMcap={int(BOT_CALL_MAX_MARKET_CAP)}"
        headers = {
            "x-api-key": JUPITER_API_KEY
        }
        
        print(f"[DEBUG] Fetching top traded tokens (1h) from Jupiter API (mcap: ${BOT_CALL_MIN_MARKET_CAP:,.0f} - ${BOT_CALL_MAX_MARKET_CAP:,.0f})...")
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 429:
                print("[WARN] Jupiter API rate limited, skipping...")
                return []
            
            response.raise_for_status()
            data = await response.json()
            
            # Jupiter API returns list of tokens (already Solana-only)
            tokens = data if isinstance(data, list) else []
            print(f"[DEBUG] Jupiter API returned {len(tokens)} token(s)")
            
            # Log first few tokens for debugging
            for i, token in enumerate(tokens[:10], 1):
                token_id = token.get("id") or token.get("address", "N/A")
                token_symbol = token.get("symbol", "UNKNOWN")
                token_mcap = token.get("mcap") or token.get("fdv") or token.get("marketCap") or 0
                try:
                    mcap_val = float(token_mcap) if token_mcap else 0
                    print(f"[DEBUG]   {i}. {token_symbol} ({token_id[:8]}...): mcap=${mcap_val:,.0f}")
                except:
                    print(f"[DEBUG]   {i}. {token_symbol} ({token_id[:8]}...): mcap=N/A")
            
            # Log structure of first token to see available fields (especially fees)
            if tokens and len(tokens) > 0:
                first_token = tokens[0]
                print(f"[DEBUG] First token structure keys: {list(first_token.keys())}")
                if "stats24h" in first_token and isinstance(first_token["stats24h"], dict):
                    print(f"[DEBUG] stats24h keys: {list(first_token['stats24h'].keys())}")
                if "stats1h" in first_token and isinstance(first_token["stats1h"], dict):
                    print(f"[DEBUG] stats1h keys: {list(first_token['stats1h'].keys())}")
            
            # Check if specific tokens are in the response (for debugging)
            target_tokens = ["3k29upUrDXNF3cuRYArqUKw8AtUNWSqbfZfRvB6fBAGS", "GLBV9FAMhULQpD6iQMGBSchD9s1Hdzd79VqetVjgpump"]
            for target_id in target_tokens:
                found = any((token.get("id") or token.get("address", "")) == target_id for token in tokens)
                if found:
                    print(f"[DEBUG] ✅ Target token {target_id[:8]}... DITEMUKAN di Jupiter API response!")
                else:
                    print(f"[DEBUG] ❌ Target token {target_id[:8]}... TIDAK DITEMUKAN di Jupiter API response")
            
            if not tokens:
                return []
            
            # Get SOL price for fee conversion (try CoinGecko first, then Jupiter)
            sol_price_usd = await fetch_sol_price()
            
            qualifying_tokens = []
            now = time.time()
            
            for token in tokens:
                try:
                    # toptraded endpoint uses "id" instead of "address"
                    token_address = token.get("id") or token.get("address")
                    if not token_address or not is_valid_solana_address(token_address):
                        continue
                    
                    # Get token metadata
                    token_name = token.get("name", "Unknown")
                    token_symbol = token.get("symbol", "UNKNOWN")
                    
                    # Log token being processed
                    print(f"[DEBUG] Processing token: {token_symbol} ({token_address[:8]}...)")
                    
                    # Get market cap from token data (toptraded uses "mcap" or "fdv")
                    market_cap = None
                    if "mcap" in token:
                        try:
                            market_cap = float(token["mcap"])
                        except (ValueError, TypeError):
                            pass
                    if not market_cap and "fdv" in token:
                        try:
                            market_cap = float(token["fdv"])
                        except (ValueError, TypeError):
                            pass
                    if not market_cap and "marketCap" in token:
                        try:
                            market_cap = float(token["marketCap"])
                        except (ValueError, TypeError):
                            pass
                    
                    # Get volume 24h from stats24h (toptraded endpoint structure)
                    volume_24h_usd = None
                    stats24h = token.get("stats24h", {})
                    stats1h = token.get("stats1h", {})
                    if isinstance(stats24h, dict):
                        buy_volume = stats24h.get("buyVolume", 0) or 0
                        sell_volume = stats24h.get("sellVolume", 0) or 0
                        try:
                            volume_24h_usd = float(buy_volume) + float(sell_volume)
                        except (ValueError, TypeError):
                            pass
                    
                    # Fallback to volume24h if stats24h not available
                    if not volume_24h_usd and "volume24h" in token:
                        try:
                            volume_24h_usd = float(token["volume24h"])
                        except (ValueError, TypeError):
                            pass
                    
                    # Get price change 1h from stats1h
                    price_change_1h = None
                    if stats1h and isinstance(stats1h, dict):
                        price_change_1h = stats1h.get("priceChange")
                        if price_change_1h is not None:
                            try:
                                price_change_1h = float(price_change_1h)
                            except (ValueError, TypeError):
                                price_change_1h = None
                    
                    # Get fees from Jupiter API response (fees are in SOL, not USD)
                    jupiter_fees_sol = None
                    
                    # Check for fees at token root level first (most common location based on API response)
                    # Priority: "fees" (most common) -> "fees24h" -> "fees_24h" -> "totalFees" -> "total_fees"
                    for fee_field in ["fees", "fees24h", "fees_24h", "totalFees", "total_fees"]:
                        if fee_field in token:
                            try:
                                jupiter_fees_sol = float(token[fee_field])
                                print(f"[DEBUG]   {token_symbol}: Found fees in token.{fee_field}: {jupiter_fees_sol:.4f} SOL")
                                break
                            except (ValueError, TypeError):
                                pass
                    
                    # Fallback: Check for fees in stats24h if not found at root level
                    if jupiter_fees_sol is None and stats24h and isinstance(stats24h, dict):
                        for fee_field in ["fees", "fees24h", "fees_24h", "totalFees", "total_fees"]:
                            if fee_field in stats24h:
                                try:
                                    jupiter_fees_sol = float(stats24h[fee_field])
                                    print(f"[DEBUG]   {token_symbol}: Found fees in stats24h.{fee_field}: {jupiter_fees_sol:.4f} SOL")
                                    break
                                except (ValueError, TypeError):
                                    pass
                    
                    # Try to get volume and fees from Meteora if enabled
                    meteora_volume = None
                    meteora_fees = None
                    if USE_METEORA_FOR_FEES:
                        meteora_volume, meteora_fees = fetch_meteora_volume_and_fees(token_address)
                    
                    # Use Meteora volume if it's higher than Jupiter volume
                    if meteora_volume and meteora_volume > (volume_24h_usd or 0):
                        volume_24h_usd = meteora_volume
                    
                    # Calculate fees: prioritize Jupiter API fees (in SOL), then Meteora (in USD), then calculate from volume
                    if jupiter_fees_sol and jupiter_fees_sol > 0:
                        # Jupiter fees are already in SOL, use directly
                        total_fees_sol = jupiter_fees_sol
                        total_fees_usd = total_fees_sol * sol_price_usd if sol_price_usd and total_fees_sol > 0 else 0
                        print(f"[DEBUG]   {token_symbol}: Using fees from Jupiter API: {total_fees_sol:.4f} SOL (${total_fees_usd:,.2f} USD)")
                    elif meteora_fees and meteora_fees > 0:
                        # Meteora fees are in USD, convert to SOL
                        total_fees_usd = meteora_fees
                        total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
                        print(f"[DEBUG]   {token_symbol}: Using fees from Meteora: {total_fees_sol:.4f} SOL (${total_fees_usd:,.2f} USD)")
                    else:
                        # Calculate fees from volume (0.3% of volume is typical for DEX fees)
                        fee_percentage = 0.003
                        total_fees_usd = volume_24h_usd * fee_percentage if volume_24h_usd else 0
                        total_fees_sol = total_fees_usd / sol_price_usd if sol_price_usd and total_fees_usd > 0 else 0
                        print(f"[DEBUG]   {token_symbol}: Calculated fees from volume (0.3%): {total_fees_sol:.4f} SOL (${total_fees_usd:,.2f} USD)")
                    
                    # Check criteria with detailed logging
                    market_cap_ok = market_cap and market_cap >= BOT_CALL_MIN_MARKET_CAP and market_cap <= BOT_CALL_MAX_MARKET_CAP
                    fees_ok = total_fees_sol >= BOT_CALL_MIN_FEES_SOL
                    price_change_1h_ok = price_change_1h is not None and price_change_1h >= BOT_CALL_MIN_PRICE_CHANGE_1H
                    
                    # Log filter check results
                    print(f"[DEBUG]   {token_symbol} filter check:")
                    mcap_str = f"${market_cap:,.0f}" if market_cap else "$0"
                    print(f"    - Market cap: {mcap_str} (min: ${BOT_CALL_MIN_MARKET_CAP:,.0f}, max: ${BOT_CALL_MAX_MARKET_CAP:,.0f}) -> {'✅' if market_cap_ok else '❌'}")
                    
                    # Show fees source
                    fees_source = "Jupiter API" if jupiter_fees_sol and jupiter_fees_sol > 0 else ("Meteora" if meteora_fees and meteora_fees > 0 else "Calculated (0.3% of volume)")
                    print(f"    - Fees: {total_fees_sol:.2f} SOL (${total_fees_usd:,.2f} USD) from {fees_source} (min: {BOT_CALL_MIN_FEES_SOL} SOL) -> {'✅' if fees_ok else '❌'}")
                    
                    price_change_str = f"{price_change_1h:.2f}%" if price_change_1h is not None else "N/A"
                    print(f"    - Price change 1h: {price_change_str} (min: {BOT_CALL_MIN_PRICE_CHANGE_1H}%) -> {'✅' if price_change_1h_ok else '❌'}")
                    
                    if not (market_cap_ok and fees_ok and price_change_1h_ok):
                        print(f"[DEBUG]   {token_symbol} TIDAK MEMENUHI kriteria, skip")
                        continue
                    
                    print(f"[DEBUG]   {token_symbol} MEMENUHI semua kriteria filter, lanjut cek Meteora pools...")
                    
                    # Get additional data
                    price_usd = token.get("usdPrice") or token.get("price") or None
                    if price_usd:
                        try:
                            price_usd = float(price_usd)
                        except (ValueError, TypeError):
                            price_usd = None
                    
                    liquidity_usd = token.get("liquidity") or None
                    if liquidity_usd:
                        try:
                            liquidity_usd = float(liquidity_usd)
                        except (ValueError, TypeError):
                            liquidity_usd = None
                    
                    # Get price change 24h from stats24h
                    price_change_24h = None
                    if stats24h and isinstance(stats24h, dict):
                        price_change_24h = stats24h.get("priceChange")
                        if price_change_24h:
                            try:
                                price_change_24h = float(price_change_24h)
                            except (ValueError, TypeError):
                                price_change_24h = None
                    
                    # Fallback to priceChange24h
                    if not price_change_24h:
                        price_change_24h = token.get("priceChange24h") or None
                        if price_change_24h:
                            try:
                                price_change_24h = float(price_change_24h)
                            except (ValueError, TypeError):
                                price_change_24h = None
                    
                    # Get created_at for reference (tidak digunakan untuk filter)
                    created_at = token.get("createdAt") or token.get("created_at") or token.get("firstPool", {}).get("createdAt")
                    
                    # Check if token has Meteora pools with min liquidity 500 USD (REQUIRED for bot call notification)
                    try:
                        print(f"[DEBUG]   {token_symbol}: Checking Meteora pools for {token_address[:8]}...")
                        meteora_pools = fetch_meteora_pools(token_address)
                        if not meteora_pools or len(meteora_pools) == 0:
                            print(f"[DEBUG]   {token_symbol}: ❌ Tidak punya pool di Meteora, skip")
                            continue
                        
                        # Check if any pool has minimum liquidity of 500 USD
                        max_liq = max([pool.get('raw_liq', 0) for pool in meteora_pools], default=0)
                        if max_liq < 500:
                            print(f"[DEBUG]   {token_symbol}: ❌ Punya {len(meteora_pools)} pool di Meteora, tapi max liquidity hanya ${max_liq:.2f} (< $500), skip")
                            continue
                        
                        print(f"[DEBUG]   {token_symbol}: ✅ Punya {len(meteora_pools)} pool di Meteora dengan max liquidity ${max_liq:.2f} (>= $500), QUALIFY!")
                    except Exception as e:
                        # Jika error saat fetch pools, skip token ini (anggap tidak punya pool)
                        print(f"[DEBUG]   {token_symbol}: ❌ Error checking Meteora pools: {e}, skip")
                        import traceback
                        traceback.print_exc()
                        continue
                    
                    qualifying_tokens.append({
                        "address": token_address,
                        "name": token_name,
                        "symbol": token_symbol,
                        "market_cap": market_cap,
                        "total_fees_sol": total_fees_sol,
                        "total_fees_usd": total_fees_usd,
                        "price_usd": price_usd,
                        "liquidity_usd": liquidity_usd,
                        "volume_24h": volume_24h_usd,
                        "price_change_24h": price_change_24h,
                        "price_change_1h": price_change_1h,
                        "created_at": created_at,
                    })
                    
                except Exception as e:
                    print(f"[ERROR] Error processing token: {e}")
                    continue
            
            # Sort by market cap
            qualifying_tokens.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
            print(f"[DEBUG] Found {len(qualifying_tokens)} qualifying token(s)")
            return qualifying_tokens
            
    except Exception as e:
        print(f"[ERROR] Failed to fetch tokens from Jupiter: {e}")
        import traceback
        traceback.print_exc()
        return []

# --- HELPER: SEND BOT CALL NOTIFICATION ---
async def send_bot_call_notification(token_data: Dict[str, object]):
    """Send notification to bot call channel for new token."""
    if not BOT_CALL_CHANNEL_ID:
        print("[WARN] BOT_CALL_CHANNEL_ID not set, skipping notification")
        return
    
    channel = bot.get_channel(BOT_CALL_CHANNEL_ID)
    if not channel:
        print(f"[WARN] Bot call channel with ID {BOT_CALL_CHANNEL_ID} not found")
        return
    
    try:
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
        
        # Try to fetch Meteora pools to get pool address for link
        meteora_pool_address = None
        try:
            pools = fetch_meteora_pools(token_address)
            if pools:
                pools.sort(key=lambda x: x.get('raw_liq', 0), reverse=True)
                top_pool = pools[0]
                meteora_pool_address = top_pool.get('address')
        except Exception as e:
            print(f"[DEBUG] Could not fetch Meteora pools for link: {e}")
        
        embed = discord.Embed(
            title=f"🆕 New Token Detected: {token_symbol}",
            description=f"**{token_name}** (`{token_symbol}`)\n\nToken baru terdeteksi dengan kriteria:\n• Market Cap: {market_cap_str}\n• Total Fees (24h): {fees_sol_str} ({fees_usd_str})",
            color=0x00ff00,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Market Cap", value=market_cap_str, inline=True)
        embed.add_field(name="Total Fees (24h)", value=f"{fees_sol_str}\n({fees_usd_str})", inline=True)
        
        if price_usd:
            embed.add_field(name="Price", value=f"${price_usd:.8f}", inline=True)
        
        if volume_24h:
            embed.add_field(name="Volume (24h)", value=_format_usd(volume_24h), inline=True)
        
        if liquidity_usd:
            embed.add_field(name="Liquidity", value=_format_usd(liquidity_usd), inline=True)
        
        price_change_1h = token_data.get("price_change_1h")
        if price_change_1h is not None:
            change_emoji_1h = "📈" if price_change_1h >= 0 else "📉"
            embed.add_field(name="Price Change (1h)", value=f"{change_emoji_1h} {price_change_1h:+.2f}%", inline=True)
        
        if price_change_24h is not None:
            change_emoji = "📈" if price_change_24h >= 0 else "📉"
            embed.add_field(name="Price Change (24h)", value=f"{change_emoji} {price_change_24h:+.2f}%", inline=True)
        
        # Add links (including Meteora with pool address if available)
        links_value = (
            f"[🔍 Solscan](https://solscan.io/token/{token_address})\n"
            f"[🪐 Jupiter](https://jup.ag/tokens/{token_address})\n"
            f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})"
        )
        
        # Add Meteora link with pool address if available, otherwise use search
        if meteora_pool_address:
            links_value += f"\n[🌊 Meteora](https://app.meteora.ag/dlmm/{meteora_pool_address})"
        else:
            # Fallback: link to Meteora search (if they have search page)
            links_value += f"\n[🌊 Meteora](https://app.meteora.ag)"
        
        embed.add_field(name="🔗 Links", value=links_value, inline=False)
        
        embed.set_footer(text=f"Token Address: {token_address[:8]}...{token_address[-8:]}")
        
        # Create button view for creating thread (admin only)
        class CreateThreadView(discord.ui.View):
            def __init__(self, token_address: str, token_symbol: str, token_name: str):
                super().__init__(timeout=None)
                self.token_address = token_address
                self.token_symbol = token_symbol
                self.token_name = token_name
            
            @discord.ui.button(label="📝 Create LP Call Thread", style=discord.ButtonStyle.primary)
            async def create_thread_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Check if user is admin/moderator
                admin_roles = ["Moderator", "admin", "Admin"]
                user_roles = [role.name for role in interaction.user.roles]
                is_admin = any(role in admin_roles for role in user_roles)
                
                if not is_admin:
                    await interaction.response.send_message("❌ Hanya admin yang bisa membuat thread!", ephemeral=True)
                    return
                
                await interaction.response.defer(ephemeral=True)
                
                try:
                    # Get LP Chat channel (untuk buat thread)
                    lp_chat_channel = bot.get_channel(THREAD_SCAN_CHANNEL_ID)
                    if not lp_chat_channel:
                        await interaction.followup.send("❌ LP Chat channel tidak ditemukan!", ephemeral=True)
                        return
                    
                    # Get LP Calls channel (untuk kirim embed info)
                    lp_calls_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
                    if not lp_calls_channel:
                        await interaction.followup.send("❌ LP Calls channel tidak ditemukan!", ephemeral=True)
                        return
                    
                    # Fetch Meteora pools first untuk dapat pair name
                    pools = []
                    try:
                        pools = fetch_meteora_pools(self.token_address)
                        pools.sort(key=lambda x: x['raw_liq'], reverse=True)
                    except Exception as e:
                        print(f"[DEBUG] Error fetching Meteora pools: {e}")
                    
                    # Create thread name (mirip !call)
                    if pools:
                        top_pool = pools[0]
                        pair_name = top_pool['pair'].replace(" ", "")
                        thread_name = f"{pair_name}"
                    else:
                        # Fallback jika pools tidak ditemukan
                        thread_name = f"{self.token_symbol}-{self.token_name[:20]}" if len(self.token_name) > 20 else f"{self.token_symbol}-{self.token_name}"
                        thread_name = thread_name.replace(" ", "").replace("/", "-")[:100]  # Discord limit
                    
                    # Create thread di LP Chat (mirip !call yang buat thread di ctx.channel)
                    thread = await lp_chat_channel.create_thread(
                        name=thread_name,
                        type=discord.ChannelType.public_thread,
                        reason=f"Thread created by {interaction.user} via bot call button",
                        auto_archive_duration=60,  # Discord minimum (akan di-override oleh task 15 menit)
                    )
                    
                    # Track thread untuk auto-archive setelah 15 menit
                    threads_to_archive[thread.id] = time.time()
                    print(f"[DEBUG] Thread {thread.id} ditambahkan ke auto-archive queue (15 menit)")
                    
                    # Send contract info embed ke thread (sama seperti !call: contract_embed dulu)
                    contract_embed = discord.Embed(
                        title=f"💬 Thread created for `{thread_name}`",
                        description=f"**Contract Address:** `{self.token_address}`",
                        color=0x3498db
                    )
                    contract_embed.add_field(
                        name="🔗 Links",
                        value=(
                            f"[🔍 Solscan](https://solscan.io/token/{self.token_address})\n"
                            f"[🪐 Jupiter](https://jup.ag/tokens/{self.token_address})\n"
                            f"[📊 GMGN](https://gmgn.ai/sol/token/{self.token_address})"
                        ),
                        inline=False
                    )
                    
                    mention_text = f"<@&{MENTION_ROLE_ID}>" if MENTION_ROLE_ID else ""
                    await thread.send(f"{mention_text}", embed=contract_embed)
                    
                    # Send Meteora pools embed ke thread (setelah contract_embed, sama seperti !call)
                    if pools:
                        desc = f"Found {len(pools)} Meteora DLMM pool untuk `{self.token_address}`\n\n"
                        for i, p in enumerate(pools[:10], 1):
                            link = f"https://app.meteora.ag/dlmm/{p['address']}"
                            desc += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"
                        
                        pool_embed = discord.Embed(
                            title=f"Meteora DLMM Pools — {thread_name}",
                            description=desc,
                            color=0x00ff00
                        )
                        pool_embed.set_footer(text=f"Requested by {interaction.user.display_name}")
                        await thread.send(embed=pool_embed)
                    
                    # Kirim embed info ke LP Calls channel (sama persis seperti !call)
                    thread_link = f"https://discord.com/channels/{interaction.guild.id}/{thread.id}"
                    top_pool_info = pools[0] if pools else None
                    
                    # Build top pool info string (avoid backslash in f-string expression)
                    if top_pool_info:
                        top_pool_str = f"**Top Pool:** {top_pool_info['pair']} ({top_pool_info['liq']})"
                    else:
                        top_pool_str = "**Top Pool:** N/A"
                    
                    info_embed = discord.Embed(
                        title=f"🧵 {thread_name}",
                        description=(
                            f"**Created by:** {interaction.user.mention}\n"
                            f"**Channel:** {lp_chat_channel.mention}\n"
                            f"**Token:** `{self.token_address[:8]}...`\n"
                            f"{top_pool_str}\n\n"
                            f"[🔗 Open Thread]({thread_link})"
                        ),
                        color=0x3498db
                    )
                    await lp_calls_channel.send(embed=info_embed)
                    
                    await interaction.followup.send(
                        f"✅ Thread berhasil dibuat di {lp_chat_channel.mention}!\n[🔗 Open Thread]({thread_link})",
                        ephemeral=True
                    )
                    
                    print(f"[DEBUG] Thread {thread.id} created by {interaction.user.name} via bot call button (in LP Chat, copied to LP Calls)")
                    
                except discord.Forbidden:
                    await interaction.followup.send("❌ Bot tidak punya izin untuk membuat thread!", ephemeral=True)
                except Exception as e:
                    await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
                    print(f"[ERROR] Error creating thread: {e}")
                    import traceback
                    traceback.print_exc()
        
        view = CreateThreadView(token_address, token_symbol, token_name)
        await channel.send(embed=embed, view=view)
        print(f"[DEBUG] Bot call notification sent for {token_symbol} ({token_address[:8]}...)")
        
        # Mark as notified (with today's date)
        today = datetime.now().strftime("%Y-%m-%d")
        bot_call_notified_tokens[token_address] = today
        save_bot_call_state()
        
        # Trigger auto-trade if enabled
        if TRADING_ENABLED and TRADING_CONFIG.get("auto_trade_from_bot_call"):
            try:
                await auto_trade_from_bot_call(token_data)
            except Exception as trade_error:
                print(f"[TRADING] Auto-trade failed for {token_symbol}: {trade_error}")
        
    except Exception as e:
        print(f"[ERROR] Failed to send bot call notification: {e}")
        import traceback
        traceback.print_exc()

# --- BACKGROUND TASK: POLL NEW TOKENS ---
@tasks.loop(minutes=BOT_CALL_POLL_INTERVAL_MINUTES or 2)
async def poll_new_tokens():
    """Poll Jupiter API for new tokens and send notifications."""
    if not BOT_CALL_CHANNEL_ID:
        return
    
    try:
        new_tokens = await fetch_new_tokens()
        
        # Get today's date
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Cleanup tokens from previous days (keep only today's)
        old_tokens = [
            addr for addr, date in bot_call_notified_tokens.items()
            if date != today
        ]
        for addr in old_tokens:
            del bot_call_notified_tokens[addr]
        if old_tokens:
            save_bot_call_state()
        
        if not new_tokens:
            print(f"[DEBUG] No qualifying tokens found after filter checks")
            return
        
        print(f"[DEBUG] Found {len(new_tokens)} qualifying token(s), checking if already notified...")
        
        # Filter out tokens already notified today with detailed logging
        unnotified_tokens = []
        for token in new_tokens:
            token_address = token.get("address")
            token_symbol = token.get("symbol", "UNKNOWN")
            
            if token_address not in bot_call_notified_tokens:
                print(f"[DEBUG]   {token_symbol} ({token_address[:8]}...) belum pernah di-notifikasi, ✅ tambahkan ke list")
                unnotified_tokens.append(token)
            elif bot_call_notified_tokens.get(token_address) != today:
                notified_date = bot_call_notified_tokens.get(token_address)
                print(f"[DEBUG]   {token_symbol} ({token_address[:8]}...) sudah di-notifikasi sebelumnya (date: {notified_date}), tapi bukan hari ini ({today}), ✅ tambahkan ke list")
                unnotified_tokens.append(token)
            else:
                print(f"[DEBUG]   {token_symbol} ({token_address[:8]}...) sudah di-notifikasi hari ini, ❌ skip")
        
        if not unnotified_tokens:
            print(f"[DEBUG] Semua token sudah di-notifikasi hari ini, tidak ada yang perlu di-notifikasi")
            return
        
        print(f"[DEBUG] {len(unnotified_tokens)} token(s) perlu di-notifikasi")
        
        # Calculate score for each token (prioritize market cap 60%, fees 40%)
        def calculate_score(token):
            market_cap = token.get("market_cap", 0) or 0
            fees_sol = token.get("total_fees_sol", 0) or 0
            market_cap_score = (market_cap / 1_000_000) * 0.6
            fees_score = (fees_sol / 100) * 0.4
            return market_cap_score + fees_score
        
        # Log all qualifying tokens before sorting
        print(f"[DEBUG] All qualifying tokens:")
        for i, token in enumerate(unnotified_tokens, 1):
            token_symbol = token.get("symbol", "UNKNOWN")
            token_address = token.get("address", "N/A")
            score = calculate_score(token)
            print(f"  {i}. {token_symbol} ({token_address[:8]}...): score={score:.2f}, mcap=${token.get('market_cap', 0):,.0f}, fees={token.get('total_fees_sol', 0):.2f} SOL")
        
        # Sort by score and get best token
        unnotified_tokens.sort(key=calculate_score, reverse=True)
        best_token = unnotified_tokens[0]
        
        print(f"[DEBUG] ✅ Selected BEST token: {best_token.get('symbol')} ({best_token.get('address', 'N/A')[:8]}...)")
        print(f"[DEBUG]    Score: {calculate_score(best_token):.2f}")
        print(f"[DEBUG]    Market cap: ${best_token.get('market_cap', 0):,.0f}")
        print(f"[DEBUG]    Fees: {best_token.get('total_fees_sol', 0):.2f} SOL")
        
        await send_bot_call_notification(best_token)
        
        # Cleanup tokens from previous days (keep only today's)
        today = datetime.now().strftime("%Y-%m-%d")
        old_tokens = [
            addr for addr, date in bot_call_notified_tokens.items()
            if date != today
        ]
        for addr in old_tokens:
            del bot_call_notified_tokens[addr]
        if old_tokens:
            save_bot_call_state()
            
    except Exception as e:
        print(f"[ERROR] Error in poll_new_tokens: {e}")
        import traceback
        traceback.print_exc()

async def trigger_bot_call_manual():
    """Manually trigger bot call notification (for testing)."""
    if not BOT_CALL_CHANNEL_ID:
        return False, "BOT_CALL_CHANNEL_ID not set"
    
    try:
        new_tokens = await fetch_new_tokens()
        
        # Get today's date
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Cleanup tokens from previous days (keep only today's)
        old_tokens = [
            addr for addr, date in bot_call_notified_tokens.items()
            if date != today
        ]
        for addr in old_tokens:
            del bot_call_notified_tokens[addr]
        if old_tokens:
            save_bot_call_state()
        
        if not new_tokens:
            return False, "No tokens found that meet the criteria"
        
        # Filter out tokens already notified today
        unnotified_tokens = [
            token for token in new_tokens
            if token.get("address") not in bot_call_notified_tokens or 
               bot_call_notified_tokens.get(token.get("address")) != today
        ]
        
        if not unnotified_tokens:
            return False, "All tokens have already been notified today"
        
        # Calculate score for each token (prioritize market cap 60%, fees 40%)
        def calculate_score(token):
            market_cap = token.get("market_cap", 0) or 0
            fees_sol = token.get("total_fees_sol", 0) or 0
            market_cap_score = (market_cap / 1_000_000) * 0.6
            fees_score = (fees_sol / 100) * 0.4
            return market_cap_score + fees_score
        
        # Sort by score and get best token
        unnotified_tokens.sort(key=calculate_score, reverse=True)
        best_token = unnotified_tokens[0]
        
        print(f"[DEBUG] Manual trigger - Selected BEST token: {best_token.get('symbol')} (score: {calculate_score(best_token):.2f}, market cap: ${best_token.get('market_cap', 0):,.0f}, fees: {best_token.get('total_fees_sol', 0):.2f} SOL)")
        
        await send_bot_call_notification(best_token)
        
        return True, f"Sent notification for {best_token.get('symbol')} ({best_token.get('name')})"
        
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        print(f"[ERROR] Error in trigger_bot_call_manual: {e}")
        import traceback
        traceback.print_exc()
        return False, error_msg

# ============================================================================
# --- TRADING BOT: BACKGROUND TASK & COMMANDS ---
# ============================================================================

@tasks.loop(seconds=15)  # Check every 15 seconds (configurable via TRADING_CHECK_INTERVAL)
async def monitor_trading_positions():
    """Background task to monitor active trading positions and auto-sell at TP/SL."""
    if not TRADING_ENABLED or not active_positions:
        return
    
    now = time.time()
    positions_to_close = []
    
    for token_address, position in list(active_positions.items()):
        try:
            # Get current price
            current_price = await get_token_price(token_address)
            if not current_price:
                print(f"[TRADING] Could not get price for {position['token_symbol']}, skipping check")
                continue
            
            entry_price = position["entry_price_usd"]
            price_change_percent = ((current_price - entry_price) / entry_price) * 100
            
            close_reason = None
            
            # Check Take Profit
            if current_price >= position["take_profit_price"]:
                close_reason = f"take_profit ({price_change_percent:+.2f}%)"
                print(f"[TRADING] 🎯 TP HIT! {position['token_symbol']} at {price_change_percent:+.2f}%")
            
            # Check Stop Loss
            elif current_price <= position["stop_loss_price"]:
                close_reason = f"stop_loss ({price_change_percent:+.2f}%)"
                print(f"[TRADING] 🛑 SL HIT! {position['token_symbol']} at {price_change_percent:+.2f}%")
            
            # Check Max Hold Time
            elif now >= position["max_hold_until"]:
                close_reason = f"timeout ({price_change_percent:+.2f}%)"
                hold_minutes = (now - position["entry_time"]) / 60
                print(f"[TRADING] ⏰ TIMEOUT! {position['token_symbol']} after {hold_minutes:.1f} min at {price_change_percent:+.2f}%")
            
            if close_reason:
                positions_to_close.append((token_address, close_reason))
            else:
                # Log position status periodically
                hold_seconds = now - position["entry_time"]
                if hold_seconds % 60 < 20:  # Log every ~minute
                    print(f"[TRADING] 📊 {position['token_symbol']}: {price_change_percent:+.2f}% | TP: {TRADING_CONFIG['take_profit_percent']}% | SL: -{TRADING_CONFIG['stop_loss_percent']}%")
                    
        except Exception as e:
            print(f"[TRADING] Error monitoring {token_address[:8]}...: {e}")
    
    # Close positions that hit TP/SL/Timeout
    for token_address, reason in positions_to_close:
        try:
            position = active_positions.get(token_address)
            if not position:
                continue
                
            success, message, pnl = await close_trading_position(token_address, reason)
            
            if success:
                # Send notification
                if pnl and pnl >= 0:
                    title = "🎯 Take Profit Hit!" if "take_profit" in reason else "⏰ Position Closed"
                    color = 0x00ff00  # Green
                else:
                    title = "🛑 Stop Loss Hit!" if "stop_loss" in reason else "📉 Position Closed"
                    color = 0xff0000  # Red
                
                await send_trading_notification(
                    title=title,
                    description=f"Position closed: **{reason}**\nTx: `{message[:16]}...`",
                    color=color,
                    position=position,
                    pnl=pnl
                )
            else:
                print(f"[TRADING] Failed to close position: {message}")
                
        except Exception as e:
            print(f"[TRADING] Error closing position {token_address[:8]}...: {e}")
            import traceback
            traceback.print_exc()

@monitor_trading_positions.before_loop
async def before_monitor_trading():
    """Wait for bot to be ready before starting trading monitor."""
    await bot.wait_until_ready()
    # Load positions on startup
    load_trading_positions()
    load_trading_history()
    print("[TRADING] Position monitor started")

async def auto_trade_from_bot_call(token_data: Dict):
    """Automatically open a trade when bot call detects a new token."""
    if not TRADING_ENABLED or not TRADING_CONFIG.get("auto_trade_from_bot_call"):
        return
    
    token_address = token_data.get("address")
    token_name = token_data.get("name", "Unknown")
    token_symbol = token_data.get("symbol", "UNKNOWN")
    liquidity = token_data.get("liquidity_usd", 0)
    
    # Check minimum liquidity
    if liquidity and liquidity < TRADING_CONFIG["min_liquidity_usd"]:
        print(f"[TRADING] Skip auto-trade for {token_symbol}: liquidity ${liquidity:.0f} < min ${TRADING_CONFIG['min_liquidity_usd']:.0f}")
        return
    
    # Open position with configured amount
    amount_sol = TRADING_CONFIG["max_position_sol"]
    
    print(f"[TRADING] 🤖 Auto-trading {token_symbol} with {amount_sol} SOL...")
    
    success, result = await open_trading_position(
        token_address=token_address,
        amount_sol=amount_sol,
        token_name=token_name,
        token_symbol=token_symbol
    )
    
    if success:
        position = active_positions.get(token_address)
        await send_trading_notification(
            title=f"🛒 Auto-Buy: {token_symbol}",
            description=f"Position opened automatically from bot call\nTx: `{result[:16]}...`",
            color=0x3498db,
            position=position
        )
    else:
        print(f"[TRADING] Auto-trade failed for {token_symbol}: {result}")

# ============================================================================
# --- HYPE TRADING: BACKGROUND SCANNER TASK ---
# ============================================================================

@tasks.loop(seconds=60)  # Scan setiap 60 detik (configurable via HYPE_SCAN_INTERVAL)
async def scan_hype_tokens():
    """Background task untuk scan token dengan volume spike dan hype signals."""
    if not TRADING_ENABLED or not TRADING_CONFIG.get("hype_trading_enabled"):
        return
    
    # Check daily loss limit
    reset_daily_pnl_if_needed()
    if daily_pnl <= -TRADING_CONFIG.get("daily_loss_limit_sol", 2):
        return
    
    # Check max concurrent positions
    if len(active_positions) >= TRADING_CONFIG.get("max_concurrent_positions", 3):
        return
    
    try:
        print(f"[HYPE] 🔍 Scanning for hype tokens...")
        qualifying_tokens = await scan_for_hype_tokens()
        
        if not qualifying_tokens:
            print(f"[HYPE] No qualifying tokens found")
            return
        
        print(f"[HYPE] Found {len(qualifying_tokens)} qualifying token(s)")
        
        # Trade the best token (highest hype score)
        best_token = qualifying_tokens[0]
        symbol = best_token.get("symbol", "???")
        
        is_dry_run = TRADING_CONFIG.get("dry_run", True)
        dry_run_tag = "[DRY RUN] " if is_dry_run else ""
        
        print(f"[HYPE] {dry_run_tag}🔥 Best token: {symbol} (score: {best_token.get('hype_score', 0)})")
        
        # Execute trade (or simulate if dry run)
        success, result = await execute_hype_trade(best_token)
        
        if success:
            if is_dry_run:
                print(f"[HYPE] 🧪 DRY RUN: Would have traded {symbol} (simulated)")
            else:
                print(f"[HYPE] ✅ Successfully traded {symbol}!")
            await send_hype_notification(best_token, trade_result=result, is_dry_run=is_dry_run)
        else:
            print(f"[HYPE] ❌ Trade failed for {symbol}: {result}")
            # Still send notification about detection (without trade)
            await send_hype_notification(best_token, is_dry_run=is_dry_run)
            
    except Exception as e:
        print(f"[HYPE] Error in scan_hype_tokens: {e}")
        import traceback
        traceback.print_exc()

@scan_hype_tokens.before_loop
async def before_scan_hype():
    """Wait for bot to be ready before starting hype scanner."""
    await bot.wait_until_ready()
    # Load hype state and KOL wallets
    load_hype_state()
    load_kol_wallets()
    print("[HYPE] Hype scanner ready")

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
    """Scan thread lama di channel LP Chat dan archive yang sudah lebih dari 15 menit"""
    global threads_to_archive
    
    lp_chat_channel = bot.get_channel(THREAD_SCAN_CHANNEL_ID)
    target_channel_id = THREAD_SCAN_CHANNEL_ID
    
    # Fallback: cari channel berdasarkan nama jika ID tidak ditemukan
    if not lp_chat_channel:
        print(f"[WARN] Channel LP Chat (ID: {THREAD_SCAN_CHANNEL_ID}) tidak ditemukan, mencari berdasarkan nama...")
        for guild in bot.guilds:
            for channel in guild.text_channels:
                channel_name_lower = channel.name.lower()
                if "lp" in channel_name_lower and "chat" in channel_name_lower:
                    lp_chat_channel = channel
                    target_channel_id = channel.id
                    print(f"[DEBUG] Found channel '{channel.name}' (ID: {channel.id}) sebagai LP Chat channel")
                    break
            if lp_chat_channel:
                break
    
    if not lp_chat_channel:
        print(f"[WARN] Channel LP Chat tidak ditemukan, skip scan thread lama")
        return
    
    try:
        guild = lp_chat_channel.guild
        if not guild:
            print(f"[WARN] Guild tidak ditemukan untuk channel {THREAD_SCAN_CHANNEL_ID}")
            return
        
        # Fetch channels untuk update cache dengan threads terbaru
        try:
            await guild.fetch_channels()
            print(f"[DEBUG] Fetched channels to update thread cache")
        except Exception as e:
            print(f"[WARN] Gagal fetch_channels: {e}, lanjut dengan cache")
        
        # Kombinasi: ambil dari guild.threads DAN channel.threads untuk memastikan semua ter-cover
        all_threads = []
        seen_thread_ids = set()
        
        # Method 1: Dari guild.threads (setelah fetch_channels, ini akan lebih lengkap)
        guild_threads = list(guild.threads)
        print(f"[DEBUG] Guild has {len(guild_threads)} threads in cache")
        print(f"[DEBUG] Looking for threads with parent_id = {target_channel_id} (channel: {lp_chat_channel.name})")
        
        # Debug: cek semua thread aktif untuk melihat parent_id-nya
        threads_by_parent = {}
        for thread in guild_threads:
            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                continue
            
            parent_id = thread.parent_id
            if parent_id not in threads_by_parent:
                threads_by_parent[parent_id] = []
            threads_by_parent[parent_id].append(thread)
        
        # Print summary
        print(f"[DEBUG] Active threads by parent channel:")
        for parent_id, threads in sorted(threads_by_parent.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
            parent_channel = guild.get_channel(parent_id)
            channel_name = parent_channel.name if parent_channel else f"Unknown ({parent_id})"
            print(f"[DEBUG]   - {channel_name} (ID: {parent_id}): {len(threads)} threads")
        
        # Iterate semua threads dan filter yang dari channel LP Calls
        for thread in guild_threads:
            if not isinstance(thread, discord.Thread):
                continue
            if thread.id in seen_thread_ids:
                continue
            if thread.archived:
                continue
            
            # Cek parent_id
            if thread.parent_id == target_channel_id:
                all_threads.append(thread)
                seen_thread_ids.add(thread.id)
        
        # Method 2: Dari channel.threads langsung (untuk memastikan tidak ada yang terlewat dari cache)
        channel_threads = list(lp_chat_channel.threads)
        print(f"[DEBUG] Channel has {len(channel_threads)} threads in cache")
        for thread in channel_threads:
            if isinstance(thread, discord.Thread) and thread.id not in seen_thread_ids:
                if not thread.archived:
                    all_threads.append(thread)
                    seen_thread_ids.add(thread.id)
        
        active_threads = all_threads
        print(f"[DEBUG] Found {len(active_threads)} active (non-archived) threads in LP Chat channel")
        
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
    
    # Start bot call polling task if channel ID is set
    if BOT_CALL_CHANNEL_ID:
        if not poll_new_tokens.is_running():
            poll_new_tokens.start()
            print(f"[DEBUG] Bot call polling started (market cap: {BOT_CALL_MIN_MARKET_CAP:,.0f} - {BOT_CALL_MAX_MARKET_CAP:,.0f}, fees >= {BOT_CALL_MIN_FEES_SOL} SOL, price change 1h >= {BOT_CALL_MIN_PRICE_CHANGE_1H}%)")
    else:
        print("[WARN] BOT_CALL_CHANNEL_ID not set - bot call monitoring disabled")
    
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
    
    # Start trading position monitor task if trading is enabled
    if TRADING_ENABLED:
        if not monitor_trading_positions.is_running():
            monitor_trading_positions.start()
            print(f"[TRADING] Position monitor started (check every {TRADING_CONFIG['price_check_interval_sec']}s)")
            print(f"[TRADING] Config: TP={TRADING_CONFIG['take_profit_percent']}%, SL={TRADING_CONFIG['stop_loss_percent']}%, Max={TRADING_CONFIG['max_position_sol']} SOL")
        
        # Start hype scanner if enabled
        if TRADING_CONFIG.get("hype_trading_enabled"):
            if not scan_hype_tokens.is_running():
                scan_hype_tokens.start()
                print(f"[HYPE] Hype scanner started (scan every {TRADING_CONFIG.get('hype_scan_interval_sec', 60)}s)")
                print(f"[HYPE] Config: Vol5m>=${TRADING_CONFIG.get('min_volume_5m_usd', 50000):,.0f}, Txns>={TRADING_CONFIG.get('min_txns_5m', 50)}, Price5m>={TRADING_CONFIG.get('min_price_change_5m', 5)}%")
        else:
            print("[HYPE] Hype trading DISABLED - set HYPE_TRADING_ENABLED=true to enable")
    else:
        print("[TRADING] Trading bot DISABLED - set TRADING_ENABLED=true to enable")
    
    # Start launch tracker task
    if LAUNCH_TRACKER_ENABLED:
        if not poll_token_launches.is_running():
            poll_token_launches.start()
            print(f"[LAUNCH_TRACKER] Started (poll every {LAUNCH_TRACKER_POLL_INTERVAL_SEC}s)")
            print(f"[LAUNCH_TRACKER] Tracking {len(launch_tracker_tokens)} token(s)")
    else:
        print("[LAUNCH_TRACKER] DISABLED - set LAUNCH_TRACKER_ENABLED=true to enable")
    
    # Start ICO tracker task
    if ICO_TRACKER_ENABLED:
        if not poll_ico_tracker.is_running():
            poll_ico_tracker.start()
            print(f"[ICO_TRACKER] Started (check every 30 minutes)")
            print(f"[ICO_TRACKER] Tracking {len(ico_tracker_list)} ICO(s)")
    else:
        print("[ICO_TRACKER] DISABLED - set ICO_TRACKER_ENABLED=true to enable")

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
                rules_channel_mention = "<#1425708221175173121>"
                await channel.send(
                    f"👋 Selamat datang {member.mention}!\n\n"
                    "Welcome Lpeeps👋 Selamat datang di metina.id komunitas Liquidity Provider di Indonesia 🇮🇩. "
                    f"Biar lebih afdol baca {rules_channel_mention}. Lets grow together 🚀\n\n"
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
                                
                                # Get base fee from pool data (API returns percentage e.g. 0.2 = 0.2%, 5 = 5%)
                                base_fee_val = None
                                if 'base_fee_percentage' in pool:
                                    base_fee_percentage = pool.get('base_fee_percentage')
                                    try:
                                        if isinstance(base_fee_percentage, (int, float)):
                                            base_fee_val = float(base_fee_percentage)
                                        elif isinstance(base_fee_percentage, str):
                                            base_fee_val = float(base_fee_percentage)
                                    except (ValueError, TypeError):
                                        pass
                                
                                # Default to 5 only if base_fee not found (do NOT use 5 when value is 0.2 - int(0.2)=0 was wrongly triggering this)
                                if base_fee_val is None:
                                    base_fee_val = 5.0
                                
                                # Format for display: show as integer if whole number, else 1 decimal (e.g. 80/0.2 or 80/5)
                                if base_fee_val == int(base_fee_val):
                                    base_fee_str = str(int(base_fee_val))
                                else:
                                    base_fee_str = f"{base_fee_val:.1f}".rstrip('0').rstrip('.')
                                bin_format = f"{bin_step}/{base_fee_str}"

                                matching_pools.append({
                                    'pair': pair_name,
                                    'bin': bin_format,
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

# ============================================================================
# --- DAMM V2 POOL TRACKER FUNCTIONS ---
# ============================================================================

async def fetch_dammv2_pools(token_address: str) -> List[Dict]:
    """
    Fetch DAMM v2 pools for a token from Meteora API.
    Returns list of pools with their addresses.
    """
    global http_session
    
    if not http_session:
        http_session = aiohttp.ClientSession()
    
    pools = []
    
    try:
        # Try DAMM v2 API endpoint (Dynamic AMM v2)
        # Meteora DAMM v2 API: https://amm-v2.meteora.ag/pools
        dammv2_url = f"https://amm-v2.meteora.ag/pools?token={token_address}"
        
        async with http_session.get(dammv2_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                
                # Handle different response formats
                if isinstance(data, list):
                    for pool in data:
                        pool_address = pool.get('pool_address') or pool.get('address') or pool.get('id')
                        if pool_address:
                            pools.append({
                                "address": pool_address,
                                "type": "dammv2",
                                "token_a": pool.get('token_a_mint') or pool.get('tokenAMint'),
                                "token_b": pool.get('token_b_mint') or pool.get('tokenBMint'),
                                "liquidity": pool.get('liquidity') or pool.get('tvl', 0),
                                "volume_24h": pool.get('volume_24h') or pool.get('trade_volume_24h', 0),
                                "fee": pool.get('fee') or pool.get('fees_24h', 0),
                                "created_at": pool.get('created_at') or pool.get('pool_created_at'),
                            })
                elif isinstance(data, dict):
                    # Single pool or nested structure
                    if 'pools' in data:
                        for pool in data['pools']:
                            pool_address = pool.get('pool_address') or pool.get('address') or pool.get('id')
                            if pool_address:
                                pools.append({
                                    "address": pool_address,
                                    "type": "dammv2",
                                    "token_a": pool.get('token_a_mint') or pool.get('tokenAMint'),
                                    "token_b": pool.get('token_b_mint') or pool.get('tokenBMint'),
                                    "liquidity": pool.get('liquidity') or pool.get('tvl', 0),
                                    "volume_24h": pool.get('volume_24h', 0),
                                    "fee": pool.get('fee', 0),
                                    "created_at": pool.get('created_at'),
                                })
                    elif 'pool_address' in data or 'address' in data:
                        pool_address = data.get('pool_address') or data.get('address')
                        pools.append({
                            "address": pool_address,
                            "type": "dammv2",
                            "token_a": data.get('token_a_mint'),
                            "token_b": data.get('token_b_mint'),
                            "liquidity": data.get('liquidity', 0),
                            "volume_24h": data.get('volume_24h', 0),
                            "fee": data.get('fee', 0),
                            "created_at": data.get('created_at'),
                        })
                
                if pools:
                    print(f"[DAMM_V2] Found {len(pools)} DAMM v2 pool(s) for {token_address[:8]}...")
                    return pools
        
        # Fallback: Try alternative endpoint (pool search by mint)
        search_url = f"https://amm-v2.meteora.ag/pair/all?search_term={token_address}"
        
        async with http_session.get(search_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, list):
                    for pool in data:
                        mint_x = (pool.get('mint_x') or pool.get('token_a_mint') or '').lower()
                        mint_y = (pool.get('mint_y') or pool.get('token_b_mint') or '').lower()
                        target_lower = token_address.lower()
                        
                        if target_lower in [mint_x, mint_y]:
                            pool_address = pool.get('address') or pool.get('pool_address')
                            if pool_address:
                                pools.append({
                                    "address": pool_address,
                                    "type": "dammv2",
                                    "token_a": pool.get('mint_x') or pool.get('token_a_mint'),
                                    "token_b": pool.get('mint_y') or pool.get('token_b_mint'),
                                    "liquidity": pool.get('liquidity', 0),
                                    "volume_24h": pool.get('trade_volume_24h', 0),
                                    "fee": pool.get('fees_24h', 0),
                                    "created_at": pool.get('created_at'),
                                })
                
                if pools:
                    print(f"[DAMM_V2] Found {len(pools)} pool(s) via search for {token_address[:8]}...")
                    return pools
        
        # Try DLMM API as last resort (might redirect to DAMM v2)
        dlmm_url = 'https://dlmm-api.meteora.ag/pair/all_by_groups'
        params = {'search_term': token_address, 'limit': 10}
        
        async with http_session.get(dlmm_url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, dict) and 'groups' in data:
                    for group in data.get('groups', []):
                        for pool in group.get('pairs', []):
                            mint_x = (pool.get('mint_x') or '').lower()
                            mint_y = (pool.get('mint_y') or '').lower()
                            target_lower = token_address.lower()
                            
                            if target_lower in [mint_x, mint_y]:
                                pool_address = pool.get('address')
                                if pool_address:
                                    pools.append({
                                        "address": pool_address,
                                        "type": "dlmm",
                                        "name": pool.get('name', ''),
                                        "token_a": pool.get('mint_x'),
                                        "token_b": pool.get('mint_y'),
                                        "liquidity": pool.get('liquidity', 0),
                                        "volume_24h": pool.get('trade_volume_24h', 0),
                                        "fee": pool.get('fees_24h', 0),
                                        "bin_step": pool.get('bin_step'),
                                        "base_fee": pool.get('base_fee_percentage'),
                                    })
        
        return pools
        
    except asyncio.TimeoutError:
        print(f"[DAMM_V2] Timeout fetching pools for {token_address[:8]}...")
        return []
    except Exception as e:
        print(f"[DAMM_V2] Error fetching pools for {token_address[:8]}...: {e}")
        return []

async def send_launch_notification(token_address: str, token_data: Dict, pools: List[Dict], jupiter_info: Optional[Dict] = None):
    """Send notification when token pool is detected and tradeable."""
    
    channel_id = LAUNCH_TRACKER_CHANNEL_ID or DAMM_CHANNEL_ID or BOT_CALL_CHANNEL_ID
    if not channel_id:
        print("[LAUNCH_TRACKER] No channel configured for notifications")
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"[LAUNCH_TRACKER] Channel {channel_id} not found")
        return
    
    try:
        token_name = token_data.get("name", "Unknown")
        token_symbol = token_data.get("symbol", "UNKNOWN")
        added_by = token_data.get("added_by", "Unknown")
        added_at = token_data.get("added_at", "")
        
        # Get the best pool (highest liquidity)
        pools_sorted = sorted(pools, key=lambda x: float(x.get('liquidity', 0) or 0), reverse=True)
        top_pool = pools_sorted[0] if pools_sorted else None
        
        pool_address = top_pool.get("address") if top_pool else None
        pool_type = top_pool.get("type", "dammv2") if top_pool else "dammv2"
        liquidity = top_pool.get("liquidity", 0) if top_pool else 0
        
        # Format liquidity
        if liquidity:
            try:
                liq_val = float(liquidity)
                if liq_val >= 1_000_000:
                    liquidity_str = f"${liq_val/1_000_000:.2f}M"
                elif liq_val >= 1000:
                    liquidity_str = f"${liq_val/1000:.2f}K"
                else:
                    liquidity_str = f"${liq_val:.2f}"
            except:
                liquidity_str = "N/A"
        else:
            liquidity_str = "N/A"
        
        # Calculate time since added
        time_diff_str = "Just now"
        if added_at:
            try:
                added_timestamp = datetime.fromisoformat(added_at.replace('Z', '+00:00'))
                now = datetime.now(added_timestamp.tzinfo) if added_timestamp.tzinfo else datetime.now()
                diff = now - added_timestamp.replace(tzinfo=None) if not added_timestamp.tzinfo else now - added_timestamp
                
                total_seconds = int(diff.total_seconds())
                if total_seconds < 60:
                    time_diff_str = f"{total_seconds} detik"
                elif total_seconds < 3600:
                    time_diff_str = f"{total_seconds // 60} menit"
                else:
                    time_diff_str = f"{total_seconds // 3600} jam {(total_seconds % 3600) // 60} menit"
            except:
                pass
        
        # Jupiter tradeable status
        jupiter_status = "✅ Tradeable"
        jupiter_routes = 0
        jupiter_impact = 0
        if jupiter_info:
            jupiter_routes = jupiter_info.get("routes", 0)
            jupiter_impact = jupiter_info.get("price_impact", 0)
            if jupiter_impact > 10:
                jupiter_status = f"⚠️ High Impact ({jupiter_impact:.1f}%)"
            elif jupiter_impact > 5:
                jupiter_status = f"✅ Tradeable ({jupiter_impact:.1f}% impact)"
            else:
                jupiter_status = f"✅ Tradeable ({jupiter_routes} routes)"
        
        # Create embed
        embed = discord.Embed(
            title=f"🚀 TOKEN LAUNCHED! {token_symbol}",
            description=(
                f"**{token_name}** (`{token_symbol}`) sudah LIVE!\n\n"
                f"Pool terdeteksi dalam **{time_diff_str}** setelah tracking dimulai!\n"
                f"🪐 **Jupiter:** {jupiter_status}"
            ),
            color=0x00FF00,  # Green
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="💰 Liquidity", value=liquidity_str, inline=True)
        embed.add_field(name="📊 Pool Type", value=pool_type.upper(), inline=True)
        embed.add_field(name="⏱️ Detection Time", value=time_diff_str, inline=True)
        
        # Jupiter info field
        if jupiter_info and jupiter_info.get("tradeable"):
            embed.add_field(
                name="🪐 Jupiter Swap",
                value=f"✅ **READY TO TRADE**\nRoutes: {jupiter_routes}\nPrice Impact: {jupiter_impact:.2f}%",
                inline=True
            )
        
        # Links
        links = []
        
        # Meteora DAMM v2 link (primary)
        if pool_address:
            if pool_type == "dlmm":
                meteora_link = f"[🌊 Meteora DLMM](https://app.meteora.ag/dlmm/{pool_address})"
            else:
                meteora_link = f"[🌊 Meteora DAMM v2](https://meteora.ag/dammv2/{pool_address})"
            links.append(meteora_link)
        
        links.extend([
            f"[🔍 Solscan](https://solscan.io/token/{token_address})",
            f"[🪐 Jupiter](https://jup.ag/swap/SOL-{token_address})",
            f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})",
            f"[🦅 Birdeye](https://birdeye.so/token/{token_address}?chain=solana)"
        ])
        
        embed.add_field(name="🔗 Links", value="\n".join(links), inline=False)
        
        # Pool info
        if pool_address:
            embed.add_field(
                name="📍 Pool Address", 
                value=f"`{pool_address[:20]}...{pool_address[-8:]}`",
                inline=False
            )
        
        embed.set_footer(text=f"Token: {token_address[:12]}...{token_address[-8:]} | Tracked by: {added_by}")
        
        # Add thumbnail (optional - you can add token logo URL if available)
        # embed.set_thumbnail(url="...")
        
        # Mention role if configured
        mention_text = ""
        if MENTION_ROLE_ID:
            mention_text = f"<@&{MENTION_ROLE_ID}> "
        
        await channel.send(content=f"{mention_text}🚀 **Token Launch Detected!**", embed=embed)
        print(f"[LAUNCH_TRACKER] ✅ Sent notification for {token_symbol} ({token_address[:8]}...)")
        
    except Exception as e:
        print(f"[LAUNCH_TRACKER] Error sending notification: {e}")
        import traceback
        traceback.print_exc()

# Background task untuk poll token launches
@tasks.loop(seconds=LAUNCH_TRACKER_POLL_INTERVAL_SEC)
async def poll_token_launches():
    """Poll tracked tokens to detect when their NEW pools go live AND tradeable on Jupiter."""
    global launch_tracker_tokens, launch_detected_pools
    
    if not LAUNCH_TRACKER_ENABLED:
        return
    
    if not launch_tracker_tokens:
        return
    
    print(f"[LAUNCH_TRACKER] Scanning {len(launch_tracker_tokens)} tracked token(s)...")
    
    for token_address, token_data in list(launch_tracker_tokens.items()):
        try:
            # Skip if already notified for new pool
            if token_data.get("status") == "launched":
                continue
            
            # Skip if status is not "tracking"
            if token_data.get("status") != "tracking":
                continue
            
            token_symbol = token_data.get("symbol", "UNKNOWN")
            
            # Step 1: Check if tradeable on Jupiter first (faster check)
            jupiter_check = await check_jupiter_tradeable(token_address)
            
            if not jupiter_check.get("tradeable"):
                # Not tradeable yet, skip pool check
                print(f"[LAUNCH_TRACKER] {token_symbol}: Not tradeable on Jupiter yet")
                await asyncio.sleep(1)
                continue
            
            print(f"[LAUNCH_TRACKER] {token_symbol}: ✅ Tradeable on Jupiter! Checking pools...")
            
            # Step 2: Fetch pools for this token
            pools = await fetch_dammv2_pools(token_address)
            
            if pools:
                # Get existing pool addresses that were saved when tracking started
                existing_pools = set(token_data.get("existing_pools", []))
                
                # Find NEW pools (not in existing_pools)
                new_pools = []
                for pool in pools:
                    pool_address = pool.get("address")
                    if pool_address and pool_address not in existing_pools:
                        new_pools.append(pool)
                
                if new_pools:
                    # NEW pool found AND tradeable! Token has launched!
                    pool_address = new_pools[0].get("address")
                    pool_type = new_pools[0].get("type", "dammv2")
                    
                    print(f"[LAUNCH_TRACKER] 🚀 NEW POOL DETECTED for {token_symbol}!")
                    print(f"[LAUNCH_TRACKER]    Pool: {pool_address} (type: {pool_type})")
                    print(f"[LAUNCH_TRACKER]    Existing pools: {len(existing_pools)}, New pools: {len(new_pools)}")
                    print(f"[LAUNCH_TRACKER]    Jupiter: Tradeable with {jupiter_check.get('routes', 0)} routes")
                    
                    # Update state
                    launch_detected_pools[token_address] = pool_address
                    launch_tracker_tokens[token_address]["status"] = "launched"
                    launch_tracker_tokens[token_address]["pool_address"] = pool_address
                    launch_tracker_tokens[token_address]["launched_at"] = datetime.now(timezone.utc).isoformat()
                    launch_tracker_tokens[token_address]["jupiter_tradeable"] = True
                    save_launch_tracker_state()
                    
                    # Send notification with NEW pools and Jupiter info
                    await send_launch_notification(token_address, token_data, new_pools, jupiter_check)
                else:
                    # Tradeable but no new pool (maybe via other DEX)
                    print(f"[LAUNCH_TRACKER] {token_symbol}: Tradeable but no NEW Meteora pool yet ({len(pools)} existing)")
            else:
                # Tradeable on Jupiter but no Meteora pool
                print(f"[LAUNCH_TRACKER] {token_symbol}: Tradeable on Jupiter but no Meteora pool found")
                
                # Option: Still notify if tradeable even without Meteora pool
                # Uncomment below to enable this behavior:
                # launch_tracker_tokens[token_address]["status"] = "tradeable_no_pool"
                # save_launch_tracker_state()
            
            # Small delay between requests to avoid rate limiting
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"[LAUNCH_TRACKER] Error checking {token_address[:8]}...: {e}")
            continue

@poll_token_launches.before_loop
async def before_poll_launches():
    """Wait for bot to be ready before starting launch tracker."""
    await bot.wait_until_ready()
    load_launch_tracker_state()
    print(f"[LAUNCH_TRACKER] Started with {len(launch_tracker_tokens)} token(s) to track")

# ============================================================================
# --- ICO TRACKER BACKGROUND TASK ---
# ============================================================================

async def send_ico_notification(ico_data: Dict, notification_type: str = "daily", ico_id: str = None):
    """Send ICO notification to channel.
    notification_type: 'daily', 'hour_warning', 'ended'
    ico_id: Optional ICO ID for refresh button functionality
    """
    channel_id = ICO_TRACKER_CHANNEL_ID or DAMM_CHANNEL_ID or BOT_CALL_CHANNEL_ID
    if not channel_id:
        print("[ICO_TRACKER] No channel configured for notifications")
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"[ICO_TRACKER] Channel {channel_id} not found")
        return
    
    try:
        ico_name = ico_data.get("name", "Unknown")
        token_symbol = ico_data.get("token_symbol", "???")
        end_time_str = ico_data.get("end_time", "")
        target = ico_data.get("target", 0)
        committed = ico_data.get("committed", 0)
        url = ico_data.get("url", "")
        token_address = ico_data.get("token_address", "")
        
        # Calculate time remaining
        time_remaining_str = "N/A"
        if end_time_str:
            try:
                # Parse end_time and ensure it's treated as UTC
                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                if end_time.tzinfo is None:
                    # If no timezone info, assume it's UTC
                    end_time = end_time.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                diff = end_time - now
                
                total_seconds = int(diff.total_seconds())
                if total_seconds > 0:
                    days = total_seconds // 86400
                    hours = (total_seconds % 86400) // 3600
                    minutes = (total_seconds % 3600) // 60
                    
                    if days > 0:
                        time_remaining_str = f"{days}D {hours}H {minutes}M"
                    elif hours > 0:
                        time_remaining_str = f"{hours}H {minutes}M"
                    else:
                        time_remaining_str = f"{minutes} menit"
                else:
                    time_remaining_str = "ENDED"
            except:
                pass
        
        # Format target and committed
        target_str = f"${target:,.0f}" if target else "N/A"
        committed_str = f"${committed:,.0f}" if committed else "N/A"
        progress_pct = (committed / target * 100) if target and committed else 0
        
        # Create embed based on notification type
        if notification_type == "hour_warning":
            title = f"⏰ ICO ENDING SOON! {token_symbol}"
            description = (
                f"**{ico_name}** ICO akan berakhir dalam **{time_remaining_str}**!\n\n"
                f"🚨 **LAST CHANCE TO PARTICIPATE!**"
            )
            color = 0xFF6600  # Orange
        elif notification_type == "ended":
            title = f"🏁 ICO ENDED: {token_symbol}"
            description = f"**{ico_name}** ICO sudah berakhir!"
            color = 0x888888  # Gray
        else:  # daily
            title = f"📊 ICO Update: {token_symbol}"
            description = (
                f"**{ico_name}** ICO masih berlangsung!\n\n"
                f"⏱️ Sisa waktu: **{time_remaining_str}**"
            )
            color = 0x00AAFF  # Blue
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="💰 Committed", value=committed_str, inline=True)
        embed.add_field(name="🎯 Target", value=target_str, inline=True)
        embed.add_field(name="📈 Progress", value=f"{progress_pct:.1f}%", inline=True)
        embed.add_field(name="⏱️ Time Left", value=time_remaining_str, inline=True)
        
        # Links
        links = []
        if url:
            links.append(f"[🍎 MetaDAO ICO]({url})")
        if token_address:
            links.extend([
                f"[🔍 Solscan](https://solscan.io/token/{token_address})",
                f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})"
            ])
        
        if links:
            embed.add_field(name="🔗 Links", value="\n".join(links), inline=False)
        
        embed.set_footer(text=f"ICO Tracker | {ico_name}")
        
        # Create refresh button view if ICO is still ongoing
        view = None
        if notification_type != "ended" and ico_id:
            class RefreshICOView(discord.ui.View):
                def __init__(self, ico_id: str):
                    super().__init__(timeout=None)
                    self.ico_id = ico_id
                
                @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
                async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                    await interaction.response.defer()
                    
                    try:
                        # Access global ico_tracker_list
                        global ico_tracker_list
                        
                        # Get latest ICO data
                        if self.ico_id not in ico_tracker_list:
                            await interaction.followup.send("❌ ICO tidak ditemukan lagi di tracker!", ephemeral=True)
                            return
                        
                        latest_ico_data = ico_tracker_list[self.ico_id]
                        
                        # Recalculate time remaining
                        end_time_str = latest_ico_data.get("end_time", "")
                        time_remaining_str = "N/A"
                        total_seconds = 0
                        is_ended = False
                        
                        if end_time_str:
                            try:
                                # Parse end_time and ensure it's treated as UTC
                                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                                if end_time.tzinfo is None:
                                    # If no timezone info, assume it's UTC
                                    end_time = end_time.replace(tzinfo=timezone.utc)
                                now = datetime.now(timezone.utc)
                                diff = end_time - now
                                
                                total_seconds = int(diff.total_seconds())
                                if total_seconds > 0:
                                    days = total_seconds // 86400
                                    hours = (total_seconds % 86400) // 3600
                                    minutes = (total_seconds % 3600) // 60
                                    
                                    if days > 0:
                                        time_remaining_str = f"{days}D {hours}H {minutes}M"
                                    elif hours > 0:
                                        time_remaining_str = f"{hours}H {minutes}M"
                                    else:
                                        time_remaining_str = f"{minutes} menit"
                                else:
                                    time_remaining_str = "ENDED"
                                    is_ended = True
                            except:
                                pass
                        
                        # Get updated values
                        latest_name = latest_ico_data.get("name", "Unknown")
                        latest_symbol = latest_ico_data.get("token_symbol", "???")
                        latest_target = latest_ico_data.get("target", 0)
                        latest_committed = latest_ico_data.get("committed", 0)
                        latest_url = latest_ico_data.get("url", "")
                        latest_token_address = latest_ico_data.get("token_address", "")
                        
                        # Format updated values
                        target_str = f"${latest_target:,.0f}" if latest_target else "N/A"
                        committed_str = f"${latest_committed:,.0f}" if latest_committed else "N/A"
                        progress_pct = (latest_committed / latest_target * 100) if latest_target and latest_committed else 0
                        
                        # Update embed based on status
                        if is_ended or time_remaining_str == "ENDED":
                            new_title = f"🏁 ICO ENDED: {latest_symbol}"
                            new_description = f"**{latest_name}** ICO sudah berakhir!"
                            new_color = 0x888888  # Gray
                        elif total_seconds > 0 and total_seconds <= 3600:  # Less than 1 hour
                            new_title = f"⏰ ICO ENDING SOON! {latest_symbol}"
                            new_description = (
                                f"**{latest_name}** ICO akan berakhir dalam **{time_remaining_str}**!\n\n"
                                f"🚨 **LAST CHANCE TO PARTICIPATE!**"
                            )
                            new_color = 0xFF6600  # Orange
                        else:
                            new_title = f"📊 ICO Update: {latest_symbol}"
                            new_description = (
                                f"**{latest_name}** ICO masih berlangsung!\n\n"
                                f"⏱️ Sisa waktu: **{time_remaining_str}**"
                            )
                            new_color = 0x00AAFF  # Blue
                        
                        # Create updated embed
                        updated_embed = discord.Embed(
                            title=new_title,
                            description=new_description,
                            color=new_color,
                            timestamp=datetime.now(timezone.utc)
                        )
                        
                        updated_embed.add_field(name="💰 Committed", value=committed_str, inline=True)
                        updated_embed.add_field(name="🎯 Target", value=target_str, inline=True)
                        updated_embed.add_field(name="📈 Progress", value=f"{progress_pct:.1f}%", inline=True)
                        updated_embed.add_field(name="⏱️ Time Left", value=time_remaining_str, inline=True)
                        
                        # Links
                        links = []
                        if latest_url:
                            links.append(f"[🍎 MetaDAO ICO]({latest_url})")
                        if latest_token_address:
                            links.extend([
                                f"[🔍 Solscan](https://solscan.io/token/{latest_token_address})",
                                f"[📊 GMGN](https://gmgn.ai/sol/token/{latest_token_address})"
                            ])
                        
                        if links:
                            updated_embed.add_field(name="🔗 Links", value="\n".join(links), inline=False)
                        
                        updated_embed.set_footer(text=f"ICO Tracker | {latest_name} | Updated")
                        
                        # Update view - remove button if ended
                        if is_ended or time_remaining_str == "ENDED":
                            new_view = None
                        else:
                            new_view = self
                        
                        await interaction.message.edit(embed=updated_embed, view=new_view)
                        await interaction.followup.send("✅ ICO info diperbarui!", ephemeral=True)
                        
                    except Exception as e:
                        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
                        print(f"[ICO_TRACKER] Error refreshing ICO: {e}")
                        import traceback
                        traceback.print_exc()
            
            view = RefreshICOView(ico_id)
        
        # Mention role for hour warning
        mention_text = ""
        if notification_type == "hour_warning" and MENTION_ROLE_ID:
            mention_text = f"<@&{MENTION_ROLE_ID}> "
        
        await channel.send(content=f"{mention_text}", embed=embed, view=view)
        print(f"[ICO_TRACKER] Sent {notification_type} notification for {ico_name}")
        
    except Exception as e:
        print(f"[ICO_TRACKER] Error sending notification: {e}")
        import traceback
        traceback.print_exc()

@tasks.loop(minutes=30)  # Check setiap 30 menit
async def poll_ico_tracker():
    """Poll ICO tracker for daily notifications and hour warnings."""
    global ico_tracker_list
    
    if not ICO_TRACKER_ENABLED:
        return
    
    if not ico_tracker_list:
        return
    
    print(f"[ICO_TRACKER] Checking {len(ico_tracker_list)} tracked ICO(s)...")
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    print(f"[ICO_TRACKER] Current UTC time: {now_utc.isoformat()}")
    
    for ico_id, ico_data in list(ico_tracker_list.items()):
        try:
            end_time_str = ico_data.get("end_time", "")
            if not end_time_str:
                continue
            
            # Parse end time
            try:
                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                if end_time.tzinfo is None:
                    # If no timezone info, assume it's UTC
                    end_time = end_time.replace(tzinfo=timezone.utc)
            except:
                print(f"[ICO_TRACKER] Invalid end_time for {ico_id}")
                continue
            
            # Calculate time remaining
            diff = end_time - now_utc
            total_seconds = int(diff.total_seconds())
            
            # Debug log
            days_left = total_seconds // 86400
            hours_left = (total_seconds % 86400) // 3600
            print(f"[ICO_TRACKER] {ico_data.get('name')}: end_time={end_time_str}, remaining={days_left}d {hours_left}h ({total_seconds}s)")
            
            # Skip if already ended
            if total_seconds <= 0:
                # Send ended notification if not sent
                if not ico_data.get("ended_notified"):
                    await send_ico_notification(ico_data, "ended", ico_id)
                    ico_tracker_list[ico_id]["ended_notified"] = True
                    save_ico_tracker_state()
                continue
            
            # Check for 1 hour warning (between 30-90 minutes remaining)
            if 1800 <= total_seconds <= 5400:  # 30-90 minutes
                if not ico_data.get("hour_reminder_sent"):
                    print(f"[ICO_TRACKER] 🚨 Sending 1-hour warning for {ico_data.get('name')}")
                    await send_ico_notification(ico_data, "hour_warning", ico_id)
                    ico_tracker_list[ico_id]["hour_reminder_sent"] = True
                    save_ico_tracker_state()
            
            # Check for daily notification (once per day)
            daily_notified = ico_data.get("daily_notified_dates", [])
            if today_str not in daily_notified:
                print(f"[ICO_TRACKER] 📊 Sending daily update for {ico_data.get('name')}")
                await send_ico_notification(ico_data, "daily", ico_id)
                
                # Update notified dates
                if "daily_notified_dates" not in ico_tracker_list[ico_id]:
                    ico_tracker_list[ico_id]["daily_notified_dates"] = []
                ico_tracker_list[ico_id]["daily_notified_dates"].append(today_str)
                save_ico_tracker_state()
            
        except Exception as e:
            print(f"[ICO_TRACKER] Error processing ICO {ico_id}: {e}")
            continue

@poll_ico_tracker.before_loop
async def before_poll_ico():
    """Wait for bot to be ready before starting ICO tracker."""
    await bot.wait_until_ready()
    load_ico_tracker_state()
    print(f"[ICO_TRACKER] Started with {len(ico_tracker_list)} tracked ICO(s)")

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

@bot.tree.command(name="botcall_test", description="Trigger bot call notification manually (for testing)")
async def botcall_test(interaction: discord.Interaction):
    """Manually trigger bot call notification for testing."""
    if not BOT_CALL_CHANNEL_ID:
        await interaction.response.send_message("❌ BOT_CALL_CHANNEL_ID not set. Bot call is disabled.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        success, message = await trigger_bot_call_manual()
        if success:
            await interaction.followup.send(f"✅ {message}\n\nCheck the bot call channel to see the notification!", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

# ============================================================================
# --- TRADING BOT SLASH COMMANDS ---
# ============================================================================

def _trading_admin_check(interaction: discord.Interaction) -> bool:
    """Check if user can use trading commands (admin/moderator only)."""
    if _user_can_run_admin_actions(interaction.user):
        return True
    raise app_commands.CheckFailure("Kamu butuh izin Admin untuk menggunakan trading commands.")

def _lp_admin_check(interaction: discord.Interaction) -> bool:
    """Check if user can use LP agent commands (admin/moderator only)."""
    if _user_can_run_admin_actions(interaction.user):
        return True
    raise app_commands.CheckFailure("❌ Kamu tidak punya izin untuk menggunakan LP Agent commands. Hanya Admin yang bisa menggunakan fitur ini.")

@bot.tree.command(name="trade_buy", description="🛒 Beli token dengan SOL (ADMIN ONLY)")
@app_commands.describe(
    token_address="Solana token address untuk dibeli",
    amount_sol="Jumlah SOL untuk trading (default: max position)",
    token_symbol="Symbol token (opsional)"
)
@app_commands.check(_trading_admin_check)
async def trade_buy(
    interaction: discord.Interaction,
    token_address: str,
    amount_sol: Optional[float] = None,
    token_symbol: Optional[str] = None
):
    """Open a trading position (buy token with SOL)."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED. Set TRADING_ENABLED=true untuk mengaktifkan.", ephemeral=True)
        return
    
    if not is_valid_solana_address(token_address):
        await interaction.response.send_message("❌ Invalid Solana token address!", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        # Use max position if amount not specified
        if amount_sol is None:
            amount_sol = TRADING_CONFIG["max_position_sol"]
        
        # Get token metadata
        token_info = await fetch_token_metadata(token_address)
        token_name = token_info.get("name") or "Unknown"
        symbol = token_symbol or token_info.get("symbol") or "???"
        
        success, result = await open_trading_position(
            token_address=token_address,
            amount_sol=amount_sol,
            token_name=token_name,
            token_symbol=symbol
        )
        
        if success:
            position = active_positions.get(token_address)
            embed = discord.Embed(
                title=f"🛒 Position Opened: {symbol}",
                description=f"Berhasil beli **{symbol}** dengan **{amount_sol:.4f} SOL**",
                color=0x00ff00,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Token", value=f"**{token_name}**\n`{token_address[:12]}...`", inline=True)
            embed.add_field(name="Amount", value=f"{amount_sol:.4f} SOL", inline=True)
            embed.add_field(name="Entry Price", value=f"${position['entry_price_usd']:.8f}", inline=True)
            embed.add_field(name="Take Profit", value=f"+{TRADING_CONFIG['take_profit_percent']}%\n(${position['take_profit_price']:.8f})", inline=True)
            embed.add_field(name="Stop Loss", value=f"-{TRADING_CONFIG['stop_loss_percent']}%\n(${position['stop_loss_price']:.8f})", inline=True)
            embed.add_field(name="Max Hold", value=f"{TRADING_CONFIG['max_hold_minutes']} min", inline=True)
            embed.add_field(name="Tx", value=f"[View on Solscan](https://solscan.io/tx/{result})", inline=False)
            embed.set_footer(text=f"Positions: {len(active_positions)}/{TRADING_CONFIG['max_concurrent_positions']}")
            
            await interaction.followup.send(embed=embed)
            
            # Also notify trading channel
            await send_trading_notification(
                title=f"🛒 New Position: {symbol}",
                description=f"Opened by {interaction.user.mention}\nTx: `{result[:16]}...`",
                color=0x00ff00,
                position=position
            )
        else:
            await interaction.followup.send(f"❌ Gagal beli: {result}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()

@bot.tree.command(name="trade_sell", description="💰 Jual posisi trading (ADMIN ONLY)")
@app_commands.describe(
    token_address="Solana token address untuk dijual",
)
@app_commands.check(_trading_admin_check)
async def trade_sell(interaction: discord.Interaction, token_address: str):
    """Close a trading position (sell token for SOL)."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED.", ephemeral=True)
        return
    
    if token_address not in active_positions:
        await interaction.response.send_message("❌ Tidak ada posisi aktif untuk token ini!", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        position = active_positions.get(token_address)
        symbol = position.get("token_symbol", "???")
        
        success, result, pnl = await close_trading_position(token_address, "manual")
        
        if success:
            pnl_emoji = "🟢" if pnl and pnl >= 0 else "🔴"
            pnl_str = f"{pnl:+.4f} SOL" if pnl is not None else "N/A"
            pnl_percent = (pnl / position["entry_amount_sol"]) * 100 if pnl and position.get("entry_amount_sol") else 0
            
            embed = discord.Embed(
                title=f"💰 Position Closed: {symbol}",
                description=f"Berhasil jual **{symbol}**",
                color=0x00ff00 if pnl and pnl >= 0 else 0xff0000,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="P&L", value=f"{pnl_emoji} {pnl_str}\n({pnl_percent:+.2f}%)", inline=True)
            embed.add_field(name="Entry", value=f"{position['entry_amount_sol']:.4f} SOL", inline=True)
            embed.add_field(name="Daily P&L", value=f"{daily_pnl:+.4f} SOL", inline=True)
            embed.add_field(name="Tx", value=f"[View on Solscan](https://solscan.io/tx/{result})", inline=False)
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Gagal jual: {result}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()

@bot.tree.command(name="trade_positions", description="📊 Lihat semua posisi trading aktif")
async def trade_positions(interaction: discord.Interaction):
    """View all active trading positions."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED.", ephemeral=True)
        return
    
    if not active_positions:
        await interaction.response.send_message("📭 Tidak ada posisi aktif saat ini.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        embed = discord.Embed(
            title="📊 Active Trading Positions",
            description=f"Total: **{len(active_positions)}** posisi aktif",
            color=0x3498db,
            timestamp=datetime.now(timezone.utc)
        )
        
        for token_address, position in active_positions.items():
            symbol = position.get("token_symbol", "???")
            entry_price = position.get("entry_price_usd", 0)
            entry_sol = position.get("entry_amount_sol", 0)
            entry_time = position.get("entry_time", 0)
            
            # Get current price
            current_price = await get_token_price(token_address)
            if current_price:
                pnl_percent = ((current_price - entry_price) / entry_price) * 100
                pnl_emoji = "🟢" if pnl_percent >= 0 else "🔴"
                price_str = f"${current_price:.8f} ({pnl_emoji} {pnl_percent:+.2f}%)"
            else:
                price_str = f"${entry_price:.8f} (current: N/A)"
            
            hold_minutes = (time.time() - entry_time) / 60
            remaining_minutes = max(0, TRADING_CONFIG["max_hold_minutes"] - hold_minutes)
            
            field_value = (
                f"**Entry:** {entry_sol:.4f} SOL @ ${entry_price:.8f}\n"
                f"**Current:** {price_str}\n"
                f"**TP:** +{TRADING_CONFIG['take_profit_percent']}% | **SL:** -{TRADING_CONFIG['stop_loss_percent']}%\n"
                f"**Hold:** {hold_minutes:.1f} min ({remaining_minutes:.0f} min left)\n"
                f"`{token_address[:12]}...`"
            )
            
            embed.add_field(name=f"💎 {symbol}", value=field_value, inline=False)
        
        embed.set_footer(text=f"Daily P&L: {daily_pnl:+.4f} SOL | Max positions: {TRADING_CONFIG['max_concurrent_positions']}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="trade_history", description="📜 Lihat history trading (closed positions)")
@app_commands.describe(limit="Jumlah trade terakhir yang ditampilkan (default: 10)")
async def trade_history_cmd(interaction: discord.Interaction, limit: int = 10):
    """View trading history."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED.", ephemeral=True)
        return
    
    if not trading_history:
        await interaction.response.send_message("📭 Belum ada history trading.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Get last N trades
        recent_trades = trading_history[-limit:][::-1]  # Newest first
        
        total_pnl = sum(t.get("pnl_sol", 0) for t in trading_history)
        wins = sum(1 for t in trading_history if t.get("pnl_sol", 0) >= 0)
        losses = len(trading_history) - wins
        win_rate = (wins / len(trading_history)) * 100 if trading_history else 0
        
        embed = discord.Embed(
            title="📜 Trading History",
            description=(
                f"**Total Trades:** {len(trading_history)}\n"
                f"**Win Rate:** {win_rate:.1f}% ({wins}W / {losses}L)\n"
                f"**Total P&L:** {total_pnl:+.4f} SOL"
            ),
            color=0x00ff00 if total_pnl >= 0 else 0xff0000,
            timestamp=datetime.now(timezone.utc)
        )
        
        for trade in recent_trades[:5]:  # Show max 5 in embed
            symbol = trade.get("token_symbol", "???")
            pnl = trade.get("pnl_sol", 0)
            pnl_percent = trade.get("pnl_percent", 0)
            reason = trade.get("close_reason", "unknown")
            exit_time = trade.get("exit_time", 0)
            
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            time_str = datetime.fromtimestamp(exit_time).strftime("%m/%d %H:%M") if exit_time else "N/A"
            
            embed.add_field(
                name=f"{pnl_emoji} {symbol}",
                value=f"**P&L:** {pnl:+.4f} SOL ({pnl_percent:+.2f}%)\n**Reason:** {reason}\n**Time:** {time_str}",
                inline=True
            )
        
        embed.set_footer(text=f"Showing last {len(recent_trades)} trades")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="trade_config", description="⚙️ Lihat/ubah konfigurasi trading")
@app_commands.describe(
    take_profit="Target take profit % (kosongkan untuk lihat saja)",
    stop_loss="Stop loss % (kosongkan untuk lihat saja)",
    max_sol="Max SOL per trade (kosongkan untuk lihat saja)",
    max_hold="Max hold time dalam menit (kosongkan untuk lihat saja)"
)
@app_commands.check(_trading_admin_check)
async def trade_config_cmd(
    interaction: discord.Interaction,
    take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    max_sol: Optional[float] = None,
    max_hold: Optional[int] = None
):
    """View or update trading configuration."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED.", ephemeral=True)
        return
    
    # Update config if any parameter provided
    updated = []
    if take_profit is not None and take_profit > 0:
        TRADING_CONFIG["take_profit_percent"] = take_profit
        updated.append(f"Take Profit: {take_profit}%")
    
    if stop_loss is not None and stop_loss > 0:
        TRADING_CONFIG["stop_loss_percent"] = stop_loss
        updated.append(f"Stop Loss: {stop_loss}%")
    
    if max_sol is not None and max_sol > 0:
        TRADING_CONFIG["max_position_sol"] = max_sol
        updated.append(f"Max Position: {max_sol} SOL")
    
    if max_hold is not None and max_hold > 0:
        TRADING_CONFIG["max_hold_minutes"] = max_hold
        updated.append(f"Max Hold: {max_hold} min")
    
    embed = discord.Embed(
        title="⚙️ Trading Configuration",
        color=0x9b59b6,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(name="Take Profit", value=f"+{TRADING_CONFIG['take_profit_percent']}%", inline=True)
    embed.add_field(name="Stop Loss", value=f"-{TRADING_CONFIG['stop_loss_percent']}%", inline=True)
    embed.add_field(name="Max Position", value=f"{TRADING_CONFIG['max_position_sol']} SOL", inline=True)
    embed.add_field(name="Max Concurrent", value=f"{TRADING_CONFIG['max_concurrent_positions']} positions", inline=True)
    embed.add_field(name="Max Hold Time", value=f"{TRADING_CONFIG['max_hold_minutes']} min", inline=True)
    embed.add_field(name="Slippage", value=f"{TRADING_CONFIG['slippage_bps'] / 100}%", inline=True)
    embed.add_field(name="Daily Loss Limit", value=f"{TRADING_CONFIG['daily_loss_limit_sol']} SOL", inline=True)
    embed.add_field(name="Auto Trade", value="✅ ON" if TRADING_CONFIG.get("auto_trade_from_bot_call") else "❌ OFF", inline=True)
    embed.add_field(name="Min Liquidity", value=f"${TRADING_CONFIG['min_liquidity_usd']:,.0f}", inline=True)
    
    if updated:
        embed.description = f"✅ **Updated:** {', '.join(updated)}"
    
    embed.set_footer(text=f"Active positions: {len(active_positions)} | Daily P&L: {daily_pnl:+.4f} SOL")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="trade_toggle_auto", description="🔄 Toggle auto-trade dari bot call ON/OFF")
@app_commands.check(_trading_admin_check)
async def trade_toggle_auto(interaction: discord.Interaction):
    """Toggle auto-trading from bot call notifications."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED.", ephemeral=True)
        return
    
    current = TRADING_CONFIG.get("auto_trade_from_bot_call", False)
    TRADING_CONFIG["auto_trade_from_bot_call"] = not current
    new_state = TRADING_CONFIG["auto_trade_from_bot_call"]
    
    emoji = "✅" if new_state else "❌"
    status = "ON" if new_state else "OFF"
    
    await interaction.response.send_message(
        f"{emoji} Auto-trade dari bot call sekarang **{status}**\n\n"
        f"Ketika {status}:\n"
        + ("• Bot akan otomatis beli token saat bot call mendeteksi token baru\n" if new_state else "• Bot TIDAK akan otomatis trading\n")
        + f"• Max position: {TRADING_CONFIG['max_position_sol']} SOL\n"
        + f"• TP: +{TRADING_CONFIG['take_profit_percent']}% | SL: -{TRADING_CONFIG['stop_loss_percent']}%",
        ephemeral=True
    )

@trade_buy.error
@trade_sell.error
@trade_config_cmd.error
@trade_toggle_auto.error
async def trading_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menggunakan command ini. Hanya admin yang bisa trading.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

# ============================================================================
# --- HYPE TRADING SLASH COMMANDS ---
# ============================================================================

@bot.tree.command(name="dry_run", description="🧪 Toggle DRY RUN mode ON/OFF (test tanpa uang asli)")
@app_commands.check(_trading_admin_check)
async def dry_run_toggle(interaction: discord.Interaction):
    """Toggle dry run (simulation) mode on/off."""
    current = TRADING_CONFIG.get("dry_run", True)
    TRADING_CONFIG["dry_run"] = not current
    new_state = TRADING_CONFIG["dry_run"]
    
    if new_state:
        emoji = "🧪"
        status = "ON"
        desc = (
            "**DRY RUN MODE AKTIF** - Trading TIDAK akan menggunakan uang asli!\n\n"
            "✅ Bot akan scan token seperti biasa\n"
            "✅ Bot akan kirim notifikasi detection\n"
            "❌ Bot TIDAK akan execute trade\n"
            "❌ TIDAK ada SOL yang dipakai\n\n"
            "Gunakan mode ini untuk testing sebelum trade beneran."
        )
    else:
        emoji = "💰"
        status = "OFF"
        desc = (
            "**⚠️ DRY RUN MODE NONAKTIF** - Trading akan menggunakan uang asli!\n\n"
            "⚠️ Bot AKAN execute trade beneran\n"
            "⚠️ SOL AKAN dipakai untuk trading\n"
            "⚠️ Pastikan kamu sudah siap!\n\n"
            f"Max per trade: {TRADING_CONFIG.get('max_position_sol', 0.5)} SOL\n"
            f"TP: +{TRADING_CONFIG.get('take_profit_percent', 7)}% | SL: -{TRADING_CONFIG.get('stop_loss_percent', 5)}%"
        )
    
    await interaction.response.send_message(
        f"{emoji} DRY RUN mode sekarang **{status}**\n\n{desc}",
        ephemeral=True
    )

@bot.tree.command(name="hype_toggle", description="🔥 Toggle hype trading ON/OFF")
@app_commands.check(_trading_admin_check)
async def hype_toggle(interaction: discord.Interaction):
    """Toggle hype trading on/off."""
    if not TRADING_ENABLED:
        await interaction.response.send_message("❌ Trading bot DISABLED. Enable TRADING_ENABLED first.", ephemeral=True)
        return
    
    current = TRADING_CONFIG.get("hype_trading_enabled", False)
    TRADING_CONFIG["hype_trading_enabled"] = not current
    new_state = TRADING_CONFIG["hype_trading_enabled"]
    
    # Start/stop the scanner task
    if new_state:
        if not scan_hype_tokens.is_running():
            scan_hype_tokens.start()
    else:
        if scan_hype_tokens.is_running():
            scan_hype_tokens.stop()
    
    emoji = "✅" if new_state else "❌"
    status = "ON" if new_state else "OFF"
    dry_run_status = "🧪 DRY RUN" if TRADING_CONFIG.get("dry_run", True) else "💰 REAL"
    
    await interaction.response.send_message(
        f"🔥 Hype Trading sekarang **{status}** {emoji}\n"
        f"Mode: **{dry_run_status}**\n\n"
        f"**Kriteria Hype Token:**\n"
        f"• Volume 5min: ≥ ${TRADING_CONFIG.get('min_volume_5m_usd', 50000):,.0f}\n"
        f"• Transaksi 5min: ≥ {TRADING_CONFIG.get('min_txns_5m', 50)}\n"
        f"• Buyers 5min: ≥ {TRADING_CONFIG.get('min_buyers_5m', 30)}\n"
        f"• Price Change 5min: {TRADING_CONFIG.get('min_price_change_5m', 5)}% - {TRADING_CONFIG.get('max_price_change_5m', 50)}%\n"
        f"• Market Cap: ${TRADING_CONFIG.get('hype_min_mcap', 100000):,.0f} - ${TRADING_CONFIG.get('hype_max_mcap', 5000000):,.0f}",
        ephemeral=True
    )

@bot.tree.command(name="hype_scan", description="🔍 Manual scan untuk hype tokens sekarang")
@app_commands.check(_trading_admin_check)
async def hype_scan_cmd(interaction: discord.Interaction):
    """Manually trigger a hype token scan. Works even if TRADING_ENABLED=false (scan only, no trade)."""
    # Note: Removed TRADING_ENABLED check - scanning doesn't require trading to be enabled
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        print("[HYPE_SCAN] Starting manual scan...")
        qualifying_tokens = await scan_for_hype_tokens()
        print(f"[HYPE_SCAN] Scan complete, found {len(qualifying_tokens) if qualifying_tokens else 0} tokens")
        
        if not qualifying_tokens:
            # No tokens found - show helpful message with current criteria
            embed = discord.Embed(
                title="🔍 Scan Complete - No Hype Tokens Found",
                description=(
                    "Tidak ada token yang memenuhi **semua** kriteria saat ini.\n\n"
                    "Ini normal karena kriteria cukup ketat untuk filter token berkualitas.\n\n"
                    "**Kriteria saat ini:**"
                ),
                color=0xffaa00,  # Yellow
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Min Volume 5m", value=f"${TRADING_CONFIG.get('min_volume_5m_usd', 50000):,.0f}", inline=True)
            embed.add_field(name="Min Txns 5m", value=f"{TRADING_CONFIG.get('min_txns_5m', 50)}", inline=True)
            embed.add_field(name="Min Buyers 5m", value=f"{TRADING_CONFIG.get('min_buyers_5m', 30)}", inline=True)
            embed.add_field(name="Price Change 5m", value=f"{TRADING_CONFIG.get('min_price_change_5m', 5)}% - {TRADING_CONFIG.get('max_price_change_5m', 50)}%", inline=True)
            embed.add_field(name="Market Cap", value=f"${TRADING_CONFIG.get('hype_min_mcap', 100000):,.0f} - ${TRADING_CONFIG.get('hype_max_mcap', 5000000):,.0f}", inline=True)
            embed.add_field(name="Liquidity", value=f"≥ ${TRADING_CONFIG.get('min_liquidity_usd', 5000):,.0f}", inline=True)
            embed.set_footer(text="💡 Tip: Gunakan /hype_config untuk adjust kriteria jika terlalu ketat")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🔥 Hype Tokens Detected",
            description=f"Ditemukan **{len(qualifying_tokens)}** token dengan hype signals!",
            color=0xff6b00,
            timestamp=datetime.now(timezone.utc)
        )
        
        for i, token in enumerate(qualifying_tokens[:5], 1):
            symbol = token.get("symbol", "???")
            name = token.get("name", "Unknown")
            score = token.get("hype_score", 0)
            vol_5m = token.get("volume_5m", 0)
            txns_5m = token.get("txns_5m", 0)
            buys_5m = token.get("buys_5m", 0)
            price_5m = token.get("price_change_5m", 0)
            mcap = token.get("market_cap", 0)
            address = token.get("address", "")
            
            # Social indicators
            social_icons = []
            if token.get("has_twitter"):
                social_icons.append("🐦")
            if token.get("has_telegram"):
                social_icons.append("📱")
            if token.get("has_website"):
                social_icons.append("🌐")
            social_str = " ".join(social_icons) if social_icons else "❌"
            
            embed.add_field(
                name=f"{i}. {symbol} (Score: {score}/100) {social_str}",
                value=(
                    f"**{name}**\n"
                    f"📊 Vol 5m: **${vol_5m:,.0f}** | Txns: {txns_5m} ({buys_5m} buys)\n"
                    f"📈 Price 5m: **{price_5m:+.1f}%** | MCap: ${mcap:,.0f}\n"
                    f"[DexScreener](https://dexscreener.com/solana/{address}) | "
                    f"[Jupiter](https://jup.ag/swap/SOL-{address}) | "
                    f"[GMGN](https://gmgn.ai/sol/token/{address})"
                ),
                inline=False
            )
        
        # Add dry run status footer
        dry_run_status = "🧪 DRY RUN MODE (tidak trade beneran)" if TRADING_CONFIG.get("dry_run", True) else "💰 REAL MODE (akan trade beneran!)"
        embed.set_footer(text=f"Mode: {dry_run_status}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        print(f"[HYPE_SCAN] Error: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="hype_test", description="🧪 Quick test - scan 1 token trending untuk test API")
@app_commands.check(_trading_admin_check)
async def hype_test_cmd(interaction: discord.Interaction):
    """Quick test to verify DexScreener API is working."""
    await interaction.response.defer(ephemeral=True)
    
    try:
        global http_session
        if not http_session:
            http_session = aiohttp.ClientSession()
        
        results = []
        
        # Test 1: Boosted tokens API
        print("[HYPE_TEST] Testing boosted tokens API...")
        boosted_url = "https://api.dexscreener.com/token-boosts/latest/v1"
        try:
            async with http_session.get(boosted_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    solana_tokens = [t for t in data[:50] if t.get("chainId") == "solana"]
                    results.append(f"✅ Boosted API: {len(solana_tokens)} Solana tokens")
                else:
                    results.append(f"❌ Boosted API: HTTP {response.status}")
        except Exception as e:
            results.append(f"❌ Boosted API: {str(e)[:50]}")
        
        # Test 2: Token profiles API
        print("[HYPE_TEST] Testing profiles API...")
        profiles_url = "https://api.dexscreener.com/token-profiles/latest/v1"
        try:
            async with http_session.get(profiles_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    solana_tokens = [t for t in data[:30] if t.get("chainId") == "solana"]
                    results.append(f"✅ Profiles API: {len(solana_tokens)} Solana tokens")
                else:
                    results.append(f"❌ Profiles API: HTTP {response.status}")
        except Exception as e:
            results.append(f"❌ Profiles API: {str(e)[:50]}")
        
        # Test 3: Get one token detail
        print("[HYPE_TEST] Testing token detail API...")
        test_token = "So11111111111111111111111111111111111111112"  # SOL
        try:
            hype_data = await get_token_hype_data(test_token)
            if hype_data:
                results.append(f"✅ Token API: Got SOL data")
            else:
                results.append(f"⚠️ Token API: No data for SOL")
        except Exception as e:
            results.append(f"❌ Token API: {str(e)[:50]}")
        
        # Create response embed
        embed = discord.Embed(
            title="🧪 Hype API Test Results",
            description="\n".join(results),
            color=0x00ff00 if all("✅" in r for r in results) else 0xffaa00,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="Current Config",
            value=(
                f"Volume 5m: ≥ ${TRADING_CONFIG.get('min_volume_5m_usd', 50000):,.0f}\n"
                f"Txns 5m: ≥ {TRADING_CONFIG.get('min_txns_5m', 50)}\n"
                f"MCap: ${TRADING_CONFIG.get('hype_min_mcap', 100000):,.0f} - ${TRADING_CONFIG.get('hype_max_mcap', 5000000):,.0f}"
            ),
            inline=False
        )
        
        embed.set_footer(text="Jika semua ✅, coba /hype_scan untuk scan full")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        print(f"[HYPE_TEST] Error: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="hype_config", description="⚙️ Lihat/ubah konfigurasi hype trading")
@app_commands.describe(
    min_volume_5m="Min volume dalam 5 menit (USD)",
    min_txns_5m="Min transaksi dalam 5 menit",
    min_price_5m="Min price change % dalam 5 menit",
    max_price_5m="Max price change % dalam 5 menit",
    min_mcap="Min market cap (USD)",
    max_mcap="Max market cap (USD)"
)
@app_commands.check(_trading_admin_check)
async def hype_config_cmd(
    interaction: discord.Interaction,
    min_volume_5m: Optional[float] = None,
    min_txns_5m: Optional[int] = None,
    min_price_5m: Optional[float] = None,
    max_price_5m: Optional[float] = None,
    min_mcap: Optional[float] = None,
    max_mcap: Optional[float] = None
):
    """View or update hype trading configuration. Works without TRADING_ENABLED."""
    # Note: Removed TRADING_ENABLED check - config can be viewed/edited anytime
    
    # Update config if parameters provided
    updated = []
    if min_volume_5m is not None:
        TRADING_CONFIG["min_volume_5m_usd"] = min_volume_5m
        updated.append(f"Min Vol 5m: ${min_volume_5m:,.0f}")
    
    if min_txns_5m is not None:
        TRADING_CONFIG["min_txns_5m"] = min_txns_5m
        updated.append(f"Min Txns 5m: {min_txns_5m}")
    
    if min_price_5m is not None:
        TRADING_CONFIG["min_price_change_5m"] = min_price_5m
        updated.append(f"Min Price 5m: {min_price_5m}%")
    
    if max_price_5m is not None:
        TRADING_CONFIG["max_price_change_5m"] = max_price_5m
        updated.append(f"Max Price 5m: {max_price_5m}%")
    
    if min_mcap is not None:
        TRADING_CONFIG["hype_min_mcap"] = min_mcap
        updated.append(f"Min MCap: ${min_mcap:,.0f}")
    
    if max_mcap is not None:
        TRADING_CONFIG["hype_max_mcap"] = max_mcap
        updated.append(f"Max MCap: ${max_mcap:,.0f}")
    
    embed = discord.Embed(
        title="🔥 Hype Trading Configuration",
        color=0xff6b00,
        timestamp=datetime.now(timezone.utc)
    )
    
    # Status
    hype_status = "✅ ON" if TRADING_CONFIG.get("hype_trading_enabled") else "❌ OFF"
    embed.add_field(name="Status", value=hype_status, inline=True)
    embed.add_field(name="Scan Interval", value=f"{TRADING_CONFIG.get('hype_scan_interval_sec', 60)}s", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    
    # Volume & Activity Filters
    embed.add_field(name="Min Volume (5m)", value=f"${TRADING_CONFIG.get('min_volume_5m_usd', 50000):,.0f}", inline=True)
    embed.add_field(name="Min Txns (5m)", value=f"{TRADING_CONFIG.get('min_txns_5m', 50)}", inline=True)
    embed.add_field(name="Min Buyers (5m)", value=f"{TRADING_CONFIG.get('min_buyers_5m', 30)}", inline=True)
    
    # Price Filters
    embed.add_field(name="Min Price Δ (5m)", value=f"{TRADING_CONFIG.get('min_price_change_5m', 5)}%", inline=True)
    embed.add_field(name="Max Price Δ (5m)", value=f"{TRADING_CONFIG.get('max_price_change_5m', 50)}%", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    
    # Market Cap Filters
    embed.add_field(name="Min Market Cap", value=f"${TRADING_CONFIG.get('hype_min_mcap', 100000):,.0f}", inline=True)
    embed.add_field(name="Max Market Cap", value=f"${TRADING_CONFIG.get('hype_max_mcap', 5000000):,.0f}", inline=True)
    embed.add_field(name="Min Liquidity", value=f"${TRADING_CONFIG.get('min_liquidity_usd', 5000):,.0f}", inline=True)
    
    # Token Age Filters
    embed.add_field(name="Max Token Age", value=f"{TRADING_CONFIG.get('max_token_age_hours', 72)}h", inline=True)
    embed.add_field(name="Min Token Age", value=f"{TRADING_CONFIG.get('min_token_age_minutes', 5)}min", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    
    if updated:
        embed.description = f"✅ **Updated:** {', '.join(updated)}"
    
    embed.set_footer(text=f"Use /hype_toggle to enable/disable hype trading")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="kol_add", description="➕ Tambah KOL wallet untuk tracking")
@app_commands.describe(
    wallet="Solana wallet address KOL",
    name="Nama KOL (contoh: 'ansem', 'blknoiz06')",
    weight="Weight/importance 1-5 (default: 3)"
)
@app_commands.check(_trading_admin_check)
async def kol_add(interaction: discord.Interaction, wallet: str, name: str, weight: int = 3):
    """Add a KOL wallet for tracking."""
    if not is_valid_solana_address(wallet):
        await interaction.response.send_message("❌ Invalid Solana wallet address!", ephemeral=True)
        return
    
    # Check if already exists
    existing = [k for k in KOL_WALLETS if k.get("wallet") == wallet]
    if existing:
        await interaction.response.send_message(f"⚠️ Wallet sudah ada: {existing[0].get('name')}", ephemeral=True)
        return
    
    weight = max(1, min(5, weight))  # Clamp 1-5
    
    KOL_WALLETS.append({
        "wallet": wallet,
        "name": name,
        "weight": weight
    })
    save_kol_wallets()
    
    await interaction.response.send_message(
        f"✅ KOL wallet ditambahkan!\n\n"
        f"**Name:** {name}\n"
        f"**Wallet:** `{wallet[:12]}...`\n"
        f"**Weight:** {'⭐' * weight}\n\n"
        f"Total KOL wallets: {len(KOL_WALLETS)}",
        ephemeral=True
    )

@bot.tree.command(name="kol_list", description="📋 Lihat daftar KOL wallets")
async def kol_list(interaction: discord.Interaction):
    """List all tracked KOL wallets."""
    if not KOL_WALLETS:
        await interaction.response.send_message(
            "📭 Belum ada KOL wallet yang di-track.\n"
            "Gunakan `/kol_add` untuk menambahkan.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="👑 KOL Wallets",
        description=f"Total: **{len(KOL_WALLETS)}** KOL wallet(s)",
        color=0xffd700,
        timestamp=datetime.now(timezone.utc)
    )
    
    for kol in KOL_WALLETS[:15]:  # Max 15
        wallet = kol.get("wallet", "")
        name = kol.get("name", "Unknown")
        weight = kol.get("weight", 3)
        
        embed.add_field(
            name=f"{'⭐' * weight} {name}",
            value=f"`{wallet[:12]}...`\n[GMGN](https://gmgn.ai/sol/address/{wallet})",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="kol_remove", description="➖ Hapus KOL wallet dari tracking")
@app_commands.describe(wallet="Solana wallet address KOL yang mau dihapus")
@app_commands.check(_trading_admin_check)
async def kol_remove(interaction: discord.Interaction, wallet: str):
    """Remove a KOL wallet from tracking."""
    global KOL_WALLETS
    
    original_count = len(KOL_WALLETS)
    KOL_WALLETS = [k for k in KOL_WALLETS if k.get("wallet") != wallet]
    
    if len(KOL_WALLETS) == original_count:
        await interaction.response.send_message("❌ Wallet tidak ditemukan!", ephemeral=True)
        return
    
    save_kol_wallets()
    
    await interaction.response.send_message(
        f"✅ KOL wallet dihapus!\n"
        f"**Wallet:** `{wallet[:12]}...`\n"
        f"Remaining: {len(KOL_WALLETS)} wallet(s)",
        ephemeral=True
    )

@dry_run_toggle.error
@hype_toggle.error
@hype_scan_cmd.error
@hype_config_cmd.error
@kol_add.error
@kol_remove.error
async def hype_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menggunakan command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

# ============================================================================
# --- LAUNCH TRACKER SLASH COMMANDS ---
# ============================================================================

@bot.tree.command(name="launch_add", description="🚀 Tambah token untuk tracking launch (detect pool DAMM v2)")
@app_commands.describe(
    token_address="Solana token contract address",
    name="Nama token (opsional)",
    symbol="Symbol token (opsional, misal: SOLO)"
)
@app_commands.check(_trading_admin_check)
async def launch_add(
    interaction: discord.Interaction,
    token_address: str,
    name: Optional[str] = None,
    symbol: Optional[str] = None
):
    """Add a token to launch tracker."""
    global launch_tracker_tokens
    
    # Validate address
    if not is_valid_solana_address(token_address):
        await interaction.response.send_message("❌ Invalid Solana address!", ephemeral=True)
        return
    
    # Check if already tracking
    if token_address in launch_tracker_tokens:
        existing = launch_tracker_tokens[token_address]
        status = existing.get("status", "tracking")
        existing_pools = existing.get("existing_pools", [])
        await interaction.response.send_message(
            f"⚠️ Token sudah di-track!\n"
            f"**Symbol:** {existing.get('symbol', 'N/A')}\n"
            f"**Status:** {status}\n"
            f"**Existing Pools:** {len(existing_pools)}\n"
            f"**Added:** {existing.get('added_at', 'N/A')[:10]}\n\n"
            f"💡 Bot akan detect pool **BARU** saja (bukan pool yang sudah ada).",
            ephemeral=True
        )
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Check existing pools - SAVE them to state (not notify for these)
    pools = await fetch_dammv2_pools(token_address)
    existing_pool_addresses = []
    
    if pools:
        # Save existing pool addresses so we don't notify for them later
        existing_pool_addresses = [p.get("address") for p in pools if p.get("address")]
        
        pool_info = []
        for p in pools[:3]:  # Show max 3 pools
            p_addr = p.get("address", "?")
            p_type = p.get("type", "unknown")
            pool_info.append(f"• `{p_addr[:16]}...` ({p_type.upper()})")
        
        pool_list = "\n".join(pool_info)
        if len(pools) > 3:
            pool_list += f"\n• ... dan {len(pools) - 3} pool lainnya"
        
        existing_msg = (
            f"\n\n⚠️ **Pool yang SUDAH ADA** (akan di-skip):\n{pool_list}\n\n"
            f"✅ Bot akan notify jika ada pool **BARU** yang dibuat!"
        )
    else:
        existing_msg = "\n\n✨ Belum ada pool. Bot akan notify saat pool pertama dibuat!"
    
    # Add to tracking with existing pools saved
    launch_tracker_tokens[token_address] = {
        "name": name or "Unknown",
        "symbol": symbol or "???",
        "added_at": datetime.now(timezone.utc).isoformat(),
        "added_by": str(interaction.user),
        "status": "tracking",
        "existing_pools": existing_pool_addresses  # Save existing pools to skip later
    }
    save_launch_tracker_state()
    
    await interaction.followup.send(
        f"✅ **Token ditambahkan ke launch tracker!**\n\n"
        f"**Token:** `{token_address}`\n"
        f"**Name:** {name or 'Unknown'}\n"
        f"**Symbol:** {symbol or '???'}\n"
        f"**Existing Pools:** {len(existing_pool_addresses)}"
        f"{existing_msg}\n"
        f"🔍 Scanning setiap **{LAUNCH_TRACKER_POLL_INTERVAL_SEC} detik**",
        ephemeral=True
    )
    print(f"[LAUNCH_TRACKER] Added token {token_address[:12]}... with {len(existing_pool_addresses)} existing pool(s)")

@bot.tree.command(name="launch_remove", description="🗑️ Hapus token dari launch tracker")
@app_commands.describe(token_address="Solana token contract address yang mau dihapus")
@app_commands.check(_trading_admin_check)
async def launch_remove(interaction: discord.Interaction, token_address: str):
    """Remove a token from launch tracker."""
    global launch_tracker_tokens, launch_detected_pools
    
    if token_address not in launch_tracker_tokens:
        await interaction.response.send_message("❌ Token tidak ditemukan di tracker!", ephemeral=True)
        return
    
    token_data = launch_tracker_tokens.pop(token_address)
    launch_detected_pools.pop(token_address, None)
    save_launch_tracker_state()
    
    await interaction.response.send_message(
        f"✅ Token dihapus dari tracker!\n"
        f"**Symbol:** {token_data.get('symbol', '???')}\n"
        f"**Status:** {token_data.get('status', 'N/A')}",
        ephemeral=True
    )
    print(f"[LAUNCH_TRACKER] Removed token {token_address[:12]}... by {interaction.user}")

@bot.tree.command(name="launch_list", description="📋 Lihat daftar token yang sedang di-track")
async def launch_list(interaction: discord.Interaction):
    """List all tracked tokens."""
    
    if not launch_tracker_tokens:
        await interaction.response.send_message(
            "📋 **Launch Tracker kosong!**\n"
            "Gunakan `/launch_add` untuk menambahkan token.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="🚀 Launch Tracker",
        description=f"**{len(launch_tracker_tokens)}** token sedang di-track",
        color=0x00ff88,
        timestamp=datetime.now(timezone.utc)
    )
    
    for address, data in list(launch_tracker_tokens.items())[:10]:
        symbol = data.get("symbol", "???")
        name = data.get("name", "Unknown")
        status = data.get("status", "tracking")
        added_at = data.get("added_at", "N/A")[:10]
        pool_address = data.get("pool_address", "")
        
        status_emoji = "🔍" if status == "tracking" else "✅" if status == "launched" else "❌"
        
        value = f"**Name:** {name}\n**Status:** {status_emoji} {status}\n**Added:** {added_at}"
        
        if pool_address:
            meteora_link = f"https://meteora.ag/dammv2/{pool_address}"
            value += f"\n[🌊 View Pool]({meteora_link})"
        
        embed.add_field(
            name=f"{symbol} - `{address[:12]}...`",
            value=value,
            inline=False
        )
    
    if len(launch_tracker_tokens) > 10:
        embed.set_footer(text=f"Showing 10 of {len(launch_tracker_tokens)} tokens")
    else:
        embed.set_footer(text=f"Poll interval: {LAUNCH_TRACKER_POLL_INTERVAL_SEC} detik")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="launch_check", description="🔍 Manual check pool untuk token tertentu")
@app_commands.describe(token_address="Solana token contract address untuk di-check")
@app_commands.check(_trading_admin_check)
async def launch_check(interaction: discord.Interaction, token_address: str):
    """Manually check if a token has pools available."""
    
    if not is_valid_solana_address(token_address):
        await interaction.response.send_message("❌ Invalid Solana address!", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        pools = await fetch_dammv2_pools(token_address)
        
        if not pools:
            await interaction.followup.send(
                f"❌ **Tidak ada pool ditemukan**\n\n"
                f"**Token:** `{token_address}`\n\n"
                f"Token belum launch atau belum ada liquidity pool di Meteora.\n"
                f"💡 Gunakan `/launch_add` untuk tracking otomatis!",
                ephemeral=True
            )
            return
        
        # Pool found!
        embed = discord.Embed(
            title="🎉 Pool Ditemukan!",
            description=f"**{len(pools)}** pool tersedia untuk token ini",
            color=0x00ff00,
            timestamp=datetime.now(timezone.utc)
        )
        
        for i, pool in enumerate(pools[:5], 1):
            pool_address = pool.get("address", "N/A")
            pool_type = pool.get("type", "dammv2")
            liquidity = pool.get("liquidity", 0)
            
            # Format liquidity
            if liquidity:
                try:
                    liq_val = float(liquidity)
                    if liq_val >= 1_000_000:
                        liq_str = f"${liq_val/1_000_000:.2f}M"
                    elif liq_val >= 1000:
                        liq_str = f"${liq_val/1000:.2f}K"
                    else:
                        liq_str = f"${liq_val:.2f}"
                except:
                    liq_str = "N/A"
            else:
                liq_str = "N/A"
            
            # Create link
            if pool_type == "dlmm":
                meteora_link = f"https://app.meteora.ag/dlmm/{pool_address}"
            else:
                meteora_link = f"https://meteora.ag/dammv2/{pool_address}"
            
            embed.add_field(
                name=f"Pool #{i} ({pool_type.upper()})",
                value=(
                    f"**Liquidity:** {liq_str}\n"
                    f"**Address:** `{pool_address[:16]}...`\n"
                    f"[🌊 Open Pool]({meteora_link})"
                ),
                inline=True
            )
        
        # Additional links
        embed.add_field(
            name="🔗 Other Links",
            value=(
                f"[🪐 Jupiter](https://jup.ag/swap/SOL-{token_address})\n"
                f"[📊 GMGN](https://gmgn.ai/sol/token/{token_address})\n"
                f"[🔍 Solscan](https://solscan.io/token/{token_address})"
            ),
            inline=False
        )
        
        embed.set_footer(text=f"Token: {token_address[:12]}...{token_address[-8:]}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        print(f"[LAUNCH_CHECK] Error: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="launch_rescan", description="🔄 Rescan existing pools untuk token yang di-track")
@app_commands.describe(token_address="Token address untuk rescan (kosongkan untuk rescan semua)")
@app_commands.check(_trading_admin_check)
async def launch_rescan(interaction: discord.Interaction, token_address: Optional[str] = None):
    """Rescan and update existing pools for tracked tokens."""
    global launch_tracker_tokens
    
    await interaction.response.defer(ephemeral=True)
    
    if token_address:
        # Rescan specific token
        if token_address not in launch_tracker_tokens:
            await interaction.followup.send("❌ Token tidak ada di tracker!", ephemeral=True)
            return
        
        tokens_to_scan = {token_address: launch_tracker_tokens[token_address]}
    else:
        # Rescan all tracking tokens
        tokens_to_scan = {k: v for k, v in launch_tracker_tokens.items() if v.get("status") == "tracking"}
    
    if not tokens_to_scan:
        await interaction.followup.send("❌ Tidak ada token yang perlu di-rescan!", ephemeral=True)
        return
    
    results = []
    for addr, data in tokens_to_scan.items():
        symbol = data.get("symbol", "???")
        try:
            pools = await fetch_dammv2_pools(addr)
            existing_addresses = [p.get("address") for p in pools if p.get("address")]
            
            # Update state
            launch_tracker_tokens[addr]["existing_pools"] = existing_addresses
            launch_tracker_tokens[addr]["status"] = "tracking"  # Reset status
            
            results.append(f"✅ **{symbol}**: {len(existing_addresses)} existing pool(s)")
            await asyncio.sleep(1)  # Rate limit
        except Exception as e:
            results.append(f"❌ **{symbol}**: Error - {str(e)[:30]}")
    
    # Reset detected pools for rescanned tokens
    for addr in tokens_to_scan:
        launch_detected_pools.pop(addr, None)
    
    save_launch_tracker_state()
    
    await interaction.followup.send(
        f"🔄 **Rescan Complete!**\n\n" + "\n".join(results) + 
        "\n\n✅ Bot akan notify jika ada pool **BARU** yang dibuat!",
        ephemeral=True
    )

@launch_add.error
@launch_remove.error
@launch_check.error
@launch_rescan.error
async def launch_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menggunakan command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

# ============================================================================
# --- ICO TRACKER SLASH COMMANDS ---
# ============================================================================

@bot.tree.command(name="ico_add", description="🍎 Tambah ICO untuk tracking (daily + 1hr reminder)")
@app_commands.describe(
    name="Nama ICO/Project (contoh: 'Ranger')",
    token_symbol="Symbol token (contoh: 'RNG')",
    days_remaining="Berapa hari lagi ICO berakhir",
    hours_remaining="Berapa jam lagi (tambahan dari hari)",
    target="Target raise dalam USD (opsional)",
    committed="Dana yang sudah committed dalam USD (opsional)",
    url="URL halaman ICO (opsional)",
    token_address="Token contract address (opsional)"
)
@app_commands.check(_trading_admin_check)
async def ico_add(
    interaction: discord.Interaction,
    name: str,
    token_symbol: str,
    days_remaining: int,
    hours_remaining: int = 0,
    target: Optional[float] = None,
    committed: Optional[float] = None,
    url: Optional[str] = None,
    token_address: Optional[str] = None
):
    """Add an ICO to the tracker."""
    global ico_tracker_list
    
    # Generate ICO ID
    ico_id = f"{token_symbol.upper()}-{int(time.time())}"
    
    # Calculate end time (using UTC)
    total_seconds = (days_remaining * 86400) + (hours_remaining * 3600)
    end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
    end_time_str = end_time.isoformat()
    
    # Create ICO entry
    ico_tracker_list[ico_id] = {
        "name": name,
        "token_symbol": token_symbol.upper(),
        "end_time": end_time_str,
        "target": target or 0,
        "committed": committed or 0,
        "url": url or "",
        "token_address": token_address or "",
        "added_at": datetime.now(timezone.utc).isoformat(),
        "added_by": str(interaction.user),
        "daily_notified_dates": [],
        "hour_reminder_sent": False,
        "ended_notified": False
    }
    save_ico_tracker_state()
    
    # Format time remaining
    if days_remaining > 0:
        time_str = f"{days_remaining}D {hours_remaining}H"
    else:
        time_str = f"{hours_remaining}H"
    
    await interaction.response.send_message(
        f"✅ **ICO ditambahkan ke tracker!**\n\n"
        f"**Project:** {name}\n"
        f"**Symbol:** {token_symbol.upper()}\n"
        f"**Ends in:** {time_str}\n"
        f"**Target:** ${target:,.0f}" if target else "" + "\n\n"
        f"📅 Bot akan kirim notifikasi:\n"
        f"• **Setiap hari** selama ICO berlangsung\n"
        f"• **1 jam sebelum** ICO berakhir\n\n"
        f"🆔 ICO ID: `{ico_id}`",
        ephemeral=True
    )
    print(f"[ICO_TRACKER] Added ICO: {name} ({token_symbol}) - ends in {time_str}")

@bot.tree.command(name="ico_remove", description="🗑️ Hapus ICO dari tracker")
@app_commands.describe(ico_id="ICO ID yang mau dihapus (lihat dari /ico_list)")
@app_commands.check(_trading_admin_check)
async def ico_remove(interaction: discord.Interaction, ico_id: str):
    """Remove an ICO from the tracker."""
    global ico_tracker_list
    
    if ico_id not in ico_tracker_list:
        await interaction.response.send_message("❌ ICO ID tidak ditemukan!", ephemeral=True)
        return
    
    ico_data = ico_tracker_list.pop(ico_id)
    save_ico_tracker_state()
    
    await interaction.response.send_message(
        f"✅ ICO dihapus dari tracker!\n"
        f"**Project:** {ico_data.get('name', 'Unknown')}\n"
        f"**Symbol:** {ico_data.get('token_symbol', '???')}",
        ephemeral=True
    )
    print(f"[ICO_TRACKER] Removed ICO: {ico_data.get('name')}")

@bot.tree.command(name="ico_list", description="📋 Lihat daftar ICO yang sedang di-track")
async def ico_list(interaction: discord.Interaction):
    """List all tracked ICOs."""
    
    if not ico_tracker_list:
        await interaction.response.send_message(
            "📋 **ICO Tracker kosong!**\n"
            "Gunakan `/ico_add` untuk menambahkan ICO.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="🍎 ICO Tracker",
        description=f"**{len(ico_tracker_list)}** ICO sedang di-track",
        color=0xFF6B6B,
        timestamp=datetime.now(timezone.utc)
    )
    
    now_utc = datetime.now(timezone.utc)
    
    for ico_id, data in list(ico_tracker_list.items())[:10]:
        name = data.get("name", "Unknown")
        symbol = data.get("token_symbol", "???")
        end_time_str = data.get("end_time", "")
        target = data.get("target", 0)
        committed = data.get("committed", 0)
        hour_sent = data.get("hour_reminder_sent", False)
        
        # Calculate time remaining
        time_remaining = "N/A"
        status_emoji = "🟢"
        if end_time_str:
            try:
                # Parse end_time and ensure it's treated as UTC
                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                if end_time.tzinfo is None:
                    # If no timezone info, assume it's UTC
                    end_time = end_time.replace(tzinfo=timezone.utc)
                diff = end_time - now_utc
                total_seconds = int(diff.total_seconds())
                
                if total_seconds <= 0:
                    time_remaining = "ENDED"
                    status_emoji = "🔴"
                elif total_seconds <= 3600:
                    time_remaining = f"{total_seconds // 60}M"
                    status_emoji = "🟠"
                else:
                    days = total_seconds // 86400
                    hours = (total_seconds % 86400) // 3600
                    if days > 0:
                        time_remaining = f"{days}D {hours}H"
                    else:
                        time_remaining = f"{hours}H"
            except:
                pass
        
        progress = (committed / target * 100) if target and committed else 0
        hour_status = "✅" if hour_sent else "⏳"
        
        embed.add_field(
            name=f"{status_emoji} {symbol} - {name}",
            value=(
                f"⏱️ **{time_remaining}** remaining\n"
                f"💰 ${committed:,.0f} / ${target:,.0f} ({progress:.0f}%)\n"
                f"🔔 1hr reminder: {hour_status}\n"
                f"🆔 `{ico_id}`"
            ),
            inline=False
        )
    
    if len(ico_tracker_list) > 10:
        embed.set_footer(text=f"Showing 10 of {len(ico_tracker_list)} ICOs")
    else:
        embed.set_footer(text="Notifications: Daily + 1 hour before end")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ico_update", description="📝 Update data ICO (committed, target)")
@app_commands.describe(
    ico_id="ICO ID yang mau di-update",
    committed="Update jumlah committed (USD)",
    target="Update target (USD)"
)
@app_commands.check(_trading_admin_check)
async def ico_update(
    interaction: discord.Interaction,
    ico_id: str,
    committed: Optional[float] = None,
    target: Optional[float] = None
):
    """Update ICO data."""
    global ico_tracker_list
    
    if ico_id not in ico_tracker_list:
        await interaction.response.send_message("❌ ICO ID tidak ditemukan!", ephemeral=True)
        return
    
    updates = []
    if committed is not None:
        ico_tracker_list[ico_id]["committed"] = committed
        updates.append(f"Committed: ${committed:,.0f}")
    
    if target is not None:
        ico_tracker_list[ico_id]["target"] = target
        updates.append(f"Target: ${target:,.0f}")
    
    if not updates:
        await interaction.response.send_message("⚠️ Tidak ada update yang dilakukan!", ephemeral=True)
        return
    
    save_ico_tracker_state()
    
    await interaction.response.send_message(
        f"✅ ICO di-update!\n\n**Updates:**\n" + "\n".join(f"• {u}" for u in updates),
        ephemeral=True
    )

@bot.tree.command(name="ico_notify", description="📢 Kirim notifikasi ICO sekarang (manual)")
@app_commands.describe(
    ico_id="ICO ID untuk kirim notifikasi",
    notification_type="Tipe notifikasi"
)
@app_commands.choices(notification_type=[
    app_commands.Choice(name="Daily Update", value="daily"),
    app_commands.Choice(name="1 Hour Warning", value="hour_warning"),
])
@app_commands.check(_trading_admin_check)
async def ico_notify(
    interaction: discord.Interaction,
    ico_id: str,
    notification_type: str = "daily"
):
    """Manually send ICO notification."""
    
    if ico_id not in ico_tracker_list:
        await interaction.response.send_message("❌ ICO ID tidak ditemukan!", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        await send_ico_notification(ico_tracker_list[ico_id], notification_type, ico_id)
        await interaction.followup.send(f"✅ Notifikasi `{notification_type}` dikirim!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@ico_add.error
@ico_remove.error
@ico_update.error
@ico_notify.error
async def ico_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Kamu tidak punya izin untuk menggunakan command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)

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

@bot.tree.command(name="metadao_fetch", description="🔍 Manual fetch & debug MetaDAO projects (admin only)")
@app_commands.check(_metadao_admin_check)
async def metadao_fetch(interaction: discord.Interaction):
    """Manually fetch MetaDAO projects and show debug info."""
    await interaction.response.defer(ephemeral=True)
    
    try:
        global http_session
        if not http_session:
            http_session = aiohttp.ClientSession()
        
        # Headers untuk request
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        
        results = []
        html = None
        
        # Step 1: Fetch HTML
        results.append("**Step 1: Fetching MetaDAO page...**")
        try:
            async with http_session.get(
                METADAO_PROJECTS_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                results.append(f"• Status: {response.status}")
                results.append(f"• Content-Type: {response.headers.get('Content-Type', 'N/A')}")
                
                if response.status == 200:
                    html = await response.text()
                    results.append(f"• HTML length: {len(html):,} chars")
                elif response.status == 429:
                    results.append("• ❌ Rate limited (429)! Coba lagi nanti.")
                else:
                    results.append(f"• ❌ HTTP Error: {response.status}")
        except Exception as e:
            results.append(f"• ❌ Request failed: {str(e)[:100]}")
        
        # Step 2: Check HTML content markers
        if html:
            results.append("\n**Step 2: Checking HTML content markers...**")
            markers = [
                ("__NEXT_DATA__", '<script id="__NEXT_DATA__"'),
                ('{"items":[', '{"items":['),
                ("timeRemaining", '"timeRemaining"'),
                ("organizationSlug", '"organizationSlug"'),
                ("fundraise", '"fundraise"'),
                ("minimumRaise", '"minimumRaise"'),
            ]
            for name, marker in markers:
                found = marker in html
                status = "✅ Found" if found else "❌ Not found"
                results.append(f"• {name}: {status}")
        
        # Step 3: Try to extract items
        if html:
            results.append("\n**Step 3: Extracting project data...**")
            items = _extract_metadao_items(html)
            results.append(f"• Raw items found: {len(items)}")
            
            if items:
                # Show first item structure
                results.append("\n**Sample item keys:**")
                first_item = items[0]
                if isinstance(first_item, dict):
                    keys = list(first_item.keys())[:15]
                    results.append(f"• Keys: {', '.join(keys)}")
        
        # Step 4: Fetch via main function
        results.append("\n**Step 4: Fetching via fetch_metadao_launches()...**")
        launches = await fetch_metadao_launches()
        results.append(f"• Active launches found: {len(launches)}")
        
        if launches:
            results.append("\n**Active Launches:**")
            for i, launch in enumerate(launches[:5], 1):
                name = launch.get("name", "Unknown")
                symbol = launch.get("token_symbol", "N/A")
                remaining = launch.get("time_remaining", 0)
                remaining_hr = remaining / 3600 if remaining else 0
                results.append(f"{i}. **{name}** (${symbol}) - {remaining_hr:.1f}h remaining")
        
        # Create embed
        embed = discord.Embed(
            title="🔍 MetaDAO Fetch Debug",
            description="\n".join(results),
            color=0x00ff00 if launches else 0xff6600,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Poll interval: {METADAO_POLL_INTERVAL_MINUTES} minutes")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        print(f"[ERROR] metadao_fetch: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@metadao_fetch.error
async def metadao_fetch_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
                    # Create button view for creating thread
                    class CreateLPThreadView(discord.ui.View):
                        def __init__(self, token_address: str, pools_data: List[Dict]):
                            super().__init__(timeout=None)
                            self.token_address = token_address
                            self.pools_data = pools_data
                        
                        @discord.ui.button(label="📝 Create LP Call Thread", style=discord.ButtonStyle.primary)
                        async def create_thread_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            # Check if user is admin/moderator
                            admin_roles = ["Moderator", "admin", "Admin"]
                            user_roles = [role.name for role in button_interaction.user.roles]
                            is_admin = any(role in admin_roles for role in user_roles)
                            
                            if not is_admin:
                                await button_interaction.response.send_message("❌ Hanya admin yang bisa membuat thread!", ephemeral=True)
                                return
                            
                            await button_interaction.response.defer(ephemeral=True)
                            
                            try:
                                # Get LP Chat channel (untuk buat thread)
                                lp_chat_channel = bot.get_channel(THREAD_SCAN_CHANNEL_ID)
                                if not lp_chat_channel:
                                    await button_interaction.followup.send("❌ LP Chat channel tidak ditemukan!", ephemeral=True)
                                    return
                                
                                # Get LP Calls channel (untuk kirim embed info)
                                lp_calls_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
                                if not lp_calls_channel:
                                    await button_interaction.followup.send("❌ LP Calls channel tidak ditemukan!", ephemeral=True)
                                    return
                                
                                # Create thread name dari pools
                                if self.pools_data:
                                    top_pool = self.pools_data[0]
                                    pair_name = top_pool['pair'].replace(" ", "")
                                    thread_name = f"{pair_name}"
                                else:
                                    thread_name = f"{self.token_address[:8]}-Pool"
                                
                                # Create thread di LP Chat
                                thread = await lp_chat_channel.create_thread(
                                    name=thread_name,
                                    type=discord.ChannelType.public_thread,
                                    reason=f"Thread created by {button_interaction.user} via pool detection button",
                                    auto_archive_duration=60,
                                )
                                
                                # Track thread untuk auto-archive setelah 15 menit
                                threads_to_archive[thread.id] = time.time()
                                print(f"[DEBUG] Thread {thread.id} ditambahkan ke auto-archive queue (15 menit)")
                                
                                # Send contract info embed ke thread
                                contract_embed = discord.Embed(
                                    title=f"💬 Thread created for `{thread_name}`",
                                    description=f"**Contract Address:** `{self.token_address}`",
                                    color=0x3498db
                                )
                                contract_embed.add_field(
                                    name="🔗 Links",
                                    value=(
                                        f"[🔍 Solscan](https://solscan.io/token/{self.token_address})\n"
                                        f"[🪐 Jupiter](https://jup.ag/tokens/{self.token_address})\n"
                                        f"[📊 GMGN](https://gmgn.ai/sol/token/{self.token_address})"
                                    ),
                                    inline=False
                                )
                                
                                mention_text = f"<@&{MENTION_ROLE_ID}>" if MENTION_ROLE_ID else ""
                                await thread.send(f"{mention_text}", embed=contract_embed)
                                
                                # Send Meteora pools embed ke thread
                                if self.pools_data:
                                    pool_desc = f"Found {len(self.pools_data)} Meteora DLMM pool untuk `{self.token_address}`\n\n"
                                    for i, p in enumerate(self.pools_data[:10], 1):
                                        link = f"https://app.meteora.ag/dlmm/{p['address']}"
                                        pool_desc += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"
                                    
                                    pool_embed = discord.Embed(
                                        title=f"Meteora DLMM Pools — {thread_name}",
                                        description=pool_desc,
                                        color=0x00ff00
                                    )
                                    pool_embed.set_footer(text=f"Requested by {button_interaction.user.display_name}")
                                    await thread.send(embed=pool_embed)
                                
                                # Kirim embed info ke LP Calls channel
                                thread_link = f"https://discord.com/channels/{button_interaction.guild.id}/{thread.id}"
                                top_pool_info = self.pools_data[0] if self.pools_data else None
                                
                                if top_pool_info:
                                    top_pool_str = f"**Top Pool:** {top_pool_info['pair']} ({top_pool_info['liq']})"
                                else:
                                    top_pool_str = "**Top Pool:** N/A"
                                
                                info_embed = discord.Embed(
                                    title=f"🧵 {thread_name}",
                                    description=(
                                        f"**Created by:** {button_interaction.user.mention}\n"
                                        f"**Channel:** {lp_chat_channel.mention}\n"
                                        f"**Token:** `{self.token_address[:8]}...`\n"
                                        f"{top_pool_str}\n\n"
                                        f"[🔗 Open Thread]({thread_link})"
                                    ),
                                    color=0x3498db
                                )
                                await lp_calls_channel.send(embed=info_embed)
                                
                                await button_interaction.followup.send(
                                    f"✅ Thread berhasil dibuat di {lp_chat_channel.mention}!\n[🔗 Open Thread]({thread_link})",
                                    ephemeral=True
                                )
                                
                                print(f"[DEBUG] Thread {thread.id} created by {button_interaction.user.name} via pool detection button")
                                
                            except discord.Forbidden:
                                await button_interaction.followup.send("❌ Bot tidak punya izin untuk membuat thread!", ephemeral=True)
                            except Exception as e:
                                await button_interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
                                print(f"[ERROR] Error creating thread: {e}")
                                import traceback
                                traceback.print_exc()
                    
                    view = CreateLPThreadView(content, pools)
                    await message.channel.send(embed=embed, view=view)
                    print(f"[DEBUG] ✅ Embed sent successfully!")
                    sys.stdout.flush()
                    
                    # Fetch and display token safety information
                    print(f"[DEBUG] Fetching token safety data for {content}")
                    safety_data = await fetch_token_safety(content)
                    if safety_data:
                        safety_embeds = create_token_safety_embeds(safety_data, content)
                        for safety_embed in safety_embeds:
                            try:
                                await message.channel.send(embed=safety_embed)
                            except Exception as e:
                                print(f"[ERROR] Failed to send safety embed: {e}")
                    else:
                        print(f"[DEBUG] No safety data available for {content}")
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
        
        # Fetch and display token safety information
        print(f"[DEBUG] Fetching token safety data for !call command: {ca}")
        safety_data = await fetch_token_safety(ca)
        if safety_data:
            safety_embeds = create_token_safety_embeds(safety_data, ca)
            for safety_embed in safety_embeds:
                try:
                    await thread.send(embed=safety_embed)
                except Exception as e:
                    print(f"[ERROR] Failed to send safety embed in !call: {e}")
        else:
            print(f"[DEBUG] No safety data available for {ca}")

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

# ============================================================================
# --- METEORA LP AGENT COMMANDS ---
# ============================================================================

# Import LP agent module
try:
    from meteora_lp_agent import get_lp_agent, MeteoraLPAgent
    LP_AGENT_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Meteora LP Agent not available: {e}")
    LP_AGENT_AVAILABLE = False

@bot.tree.command(name="lp_pool_info", description="🌊 Get informasi pool Meteora DLMM")
@app_commands.describe(pool_address="Address pool Meteora DLMM")
@app_commands.check(_lp_admin_check)
async def lp_pool_info(interaction: discord.Interaction, pool_address: str):
    """Get informasi pool Meteora DLMM"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia. Install dependencies yang diperlukan.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        agent = get_lp_agent()
        if not agent:
            await interaction.followup.send("❌ LP Agent tidak terinisialisasi. Set LP_WALLET_PRIVATE_KEY untuk enable.", ephemeral=True)
            return
        
        pool_info = await agent.get_pool_info(pool_address)
        
        if not pool_info:
            await interaction.followup.send(f"❌ Pool tidak ditemukan: `{pool_address}`", ephemeral=True)
            return
        
        # Format embed
        embed = discord.Embed(
            title=f"🌊 Meteora DLMM Pool",
            description=f"**{pool_info.get('name', 'Unknown Pool')}**",
            color=0x00ff00
        )
        
        embed.add_field(
            name="📊 Pool Info",
            value=(
                f"**Address:** `{pool_info.get('address', 'N/A')[:8]}...`\n"
                f"**Mint X:** `{pool_info.get('mint_x', 'N/A')[:8]}...`\n"
                f"**Mint Y:** `{pool_info.get('mint_y', 'N/A')[:8]}...`\n"
                f"**Bin Step:** {pool_info.get('bin_step', 'N/A')}\n"
                f"**Base Fee:** {pool_info.get('base_fee_percentage', 0)}%"
            ),
            inline=False
        )
        
        tvl = pool_info.get('tvl', 0)
        liquidity = pool_info.get('liquidity', 0)
        volume_24h = pool_info.get('volume_24h', 0)
        fees_24h = pool_info.get('fees_24h', 0)
        
        embed.add_field(
            name="💰 Metrics",
            value=(
                f"**TVL:** ${tvl:,.2f}\n"
                f"**Liquidity:** ${liquidity:,.2f}\n"
                f"**Volume 24h:** ${volume_24h:,.2f}\n"
                f"**Fees 24h:** ${fees_24h:,.2f}"
            ),
            inline=False
        )
        
        pool_link = f"https://app.meteora.ag/dlmm/{pool_info.get('address', '')}"
        embed.add_field(
            name="🔗 Links",
            value=f"[View on Meteora]({pool_link})",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_pool_info: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="lp_positions", description="📊 Lihat semua LP positions wallet")
@app_commands.describe(wallet_address="Wallet address (opsional, default: bot wallet)")
@app_commands.check(_lp_admin_check)
async def lp_positions(interaction: discord.Interaction, wallet_address: Optional[str] = None):
    """Lihat semua LP positions"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        agent = get_lp_agent()
        if not agent:
            await interaction.followup.send("❌ LP Agent tidak terinisialisasi.", ephemeral=True)
            return
        
        positions = await agent.get_lp_positions(wallet_address)
        
        if not positions:
            await interaction.followup.send(
                f"📭 Tidak ada LP positions ditemukan untuk wallet: `{wallet_address or agent.wallet_address or 'N/A'}`",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="📊 LP Positions",
            description=f"Found **{len(positions)}** position(s)",
            color=0x3498db
        )
        
        for i, pos in enumerate(positions[:10], 1):  # Limit to 10 positions
            pos_addr = pos.get('position_address', 'N/A')
            embed.add_field(
                name=f"Position #{i}",
                value=f"`{pos_addr[:8]}...`" if len(pos_addr) > 8 else f"`{pos_addr}`",
                inline=True
            )
        
        if len(positions) > 10:
            embed.set_footer(text=f"Showing 10 of {len(positions)} positions")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_positions: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="lp_returns", description="📈 Calculate estimated LP returns")
@app_commands.describe(
    pool_address="Address pool Meteora DLMM",
    token_x_amount="Amount token X (dalam SOL atau token units)",
    token_y_amount="Amount token Y (dalam SOL atau token units)",
    days="Number of days untuk projection (default: 7)"
)
@app_commands.check(_lp_admin_check)
async def lp_returns(
    interaction: discord.Interaction,
    pool_address: str,
    token_x_amount: float,
    token_y_amount: float,
    days: int = 7
):
    """Calculate estimated LP returns"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        agent = get_lp_agent()
        if not agent:
            await interaction.followup.send("❌ LP Agent tidak terinisialisasi.", ephemeral=True)
            return
        
        returns_data = await agent.calculate_lp_returns(
            pool_address,
            token_x_amount,
            token_y_amount,
            days
        )
        
        if not returns_data:
            await interaction.followup.send(f"❌ Tidak bisa calculate returns untuk pool: `{pool_address}`", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📈 Estimated LP Returns",
            description=f"Projection untuk **{days} hari**",
            color=0x00ff00
        )
        
        embed.add_field(
            name="💰 Your Position",
            value=(
                f"**TVL:** ${returns_data.get('user_tvl', 0):,.2f}\n"
                f"**Pool Share:** {returns_data.get('pool_share_pct', 0):.2f}%\n"
                f"**Pool TVL:** ${returns_data.get('pool_tvl', 0):,.2f}"
            ),
            inline=False
        )
        
        embed.add_field(
            name="📊 Returns Estimate",
            value=(
                f"**Daily Fees:** ${returns_data.get('daily_fees_estimate', 0):,.2f}\n"
                f"**{days}D Returns:** ${returns_data.get('estimated_7d_returns', 0):,.2f}\n"
                f"**Estimated APR:** {returns_data.get('estimated_apr', 0):.2f}%"
            ),
            inline=False
        )
        
        embed.add_field(
            name="📈 Pool Metrics",
            value=(
                f"**Fees 24h:** ${returns_data.get('pool_fees_24h', 0):,.2f}"
            ),
            inline=False
        )
        
        pool_link = f"https://app.meteora.ag/dlmm/{pool_address}"
        embed.add_field(
            name="🔗 Links",
            value=f"[View Pool]({pool_link})",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_returns: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="lp_monitor", description="👁️ Monitor LP position status")
@app_commands.describe(position_address="Address LP position")
@app_commands.check(_lp_admin_check)
async def lp_monitor(interaction: discord.Interaction, position_address: str):
    """Monitor LP position"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        agent = get_lp_agent()
        if not agent:
            await interaction.followup.send("❌ LP Agent tidak terinisialisasi.", ephemeral=True)
            return
        
        position_data = await agent.monitor_position(position_address)
        
        if not position_data:
            await interaction.followup.send(f"❌ Position tidak ditemukan: `{position_address}`", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="👁️ LP Position Status",
            description=f"Position: `{position_address[:8]}...`",
            color=0x3498db
        )
        
        embed.add_field(
            name="📊 Position Info",
            value=(
                f"**Address:** `{position_address[:16]}...`\n"
                f"**Lamports:** {position_data.get('lamports', 0):,}\n"
                f"**Owner:** `{position_data.get('owner', 'N/A')[:8]}...`"
            ),
            inline=False
        )
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_monitor: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="lp_add", description="➕ Add liquidity ke Meteora DLMM pool")
@app_commands.describe(
    pool_address="Address pool Meteora DLMM",
    token_x_amount="Amount token X (dalam token units)",
    token_y_amount="Amount token Y (dalam token units)",
    token_x_mint="Mint address untuk token X",
    token_y_mint="Mint address untuk token Y",
    strategy="Strategy type: spot, curve, atau bid_ask",
    min_price="Minimum price untuk range (opsional)",
    max_price="Maximum price untuk range (opsional)",
    slippage_bps="Slippage tolerance in basis points (100 = 1%, default: 100)",
    position_address="Existing position address (opsional, kosongkan untuk create new)"
)
@app_commands.check(_lp_admin_check)
async def lp_add(
    interaction: discord.Interaction,
    pool_address: str,
    token_x_amount: float,
    token_y_amount: float,
    token_x_mint: str,
    token_y_mint: str,
    strategy: str = "spot",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    slippage_bps: int = 100,
    position_address: Optional[str] = None
):
    """Add liquidity ke Meteora DLMM pool"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia.", ephemeral=True)
        return
    
    # Validate strategy
    if strategy.lower() not in ["spot", "curve", "bid_ask"]:
        await interaction.response.send_message(
            "❌ Invalid strategy. Pilih: spot, curve, atau bid_ask",
            ephemeral=True
        )
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        agent = get_lp_agent()
        if not agent or not agent.keypair:
            await interaction.followup.send(
                "❌ LP Agent tidak terinisialisasi. Set LP_WALLET_PRIVATE_KEY environment variable.",
                ephemeral=True
            )
            return
        
        # Validate addresses
        if not is_valid_solana_address(pool_address):
            await interaction.followup.send("❌ Invalid pool address!", ephemeral=True)
            return
        
        if not is_valid_solana_address(token_x_mint):
            await interaction.followup.send("❌ Invalid token X mint address!", ephemeral=True)
            return
        
        if not is_valid_solana_address(token_y_mint):
            await interaction.followup.send("❌ Invalid token Y mint address!", ephemeral=True)
            return
        
        # Validate price range
        if min_price and max_price and min_price >= max_price:
            await interaction.followup.send("❌ Min price harus lebih kecil dari max price!", ephemeral=True)
            return
        
        # Get preview dengan fee breakdown
        preview = await agent.preview_add_liquidity(
            pool_address=pool_address,
            token_x_amount=token_x_amount,
            token_y_amount=token_y_amount,
            token_x_mint=token_x_mint,
            token_y_mint=token_y_mint,
            strategy_type=strategy.lower(),
            min_price=min_price,
            max_price=max_price,
            position_address=position_address
        )
        
        if not preview:
            await interaction.followup.send("❌ Gagal mendapatkan preview. Pool mungkin tidak ditemukan.", ephemeral=True)
            return
        
        # Show fee preview dengan refundable/non-refundable info
        fees = preview.get('fees', {})
        refundable_fee = fees.get('refundable_fee_sol', 0)
        non_refundable_fee = fees.get('non_refundable_fee_sol', 0)
        total_fee = fees.get('total_fee_sol', 0)
        
        preview_embed = discord.Embed(
            title="💰 Add Liquidity Preview",
            description=f"**Pool:** {preview.get('pool_name', 'Unknown')}\n`{pool_address[:8]}...`",
            color=0x3498db
        )
        
        preview_embed.add_field(
            name="📊 Position Details",
            value=(
                f"**Token X:** {token_x_amount} (`{token_x_mint[:8]}...`)\n"
                f"**Token Y:** {token_y_amount} (`{token_y_mint[:8]}...`)\n"
                f"**Strategy:** {strategy.upper()}\n"
                f"**Bins:** {fees.get('num_bins', 0)} ({preview.get('min_bin_id', 0)} - {preview.get('max_bin_id', 0)})"
            ),
            inline=False
        )
        
        if min_price and max_price:
            preview_embed.add_field(
                name="💰 Price Range",
                value=f"**Min:** {min_price}\n**Max:** {max_price}",
                inline=False
            )
        
        # Fee breakdown
        fee_breakdown = f"**Total Required:** {total_fee:.4f} SOL\n\n"
        
        if refundable_fee > 0:
            fee_breakdown += f"✅ **Refundable:** {refundable_fee:.4f} SOL\n"
            if fees.get('position_rent_sol', 0) > 0:
                fee_breakdown += f"   • Position Rent: {fees.get('position_rent_sol', 0):.4f} SOL\n"
            if fees.get('extension_rent_sol', 0) > 0:
                fee_breakdown += f"   • Extension Rent: {fees.get('extension_rent_sol', 0):.4f} SOL\n"
            fee_breakdown += f"   _(Dapat dikembalikan saat close position)_\n\n"
        
        if non_refundable_fee > 0:
            fee_breakdown += f"❌ **Non-Refundable:** {non_refundable_fee:.4f} SOL\n"
            fee_breakdown += f"   • BinArray Creation: {fees.get('binarray_rent_sol', 0):.4f} SOL\n"
            fee_breakdown += f"   _(Tidak dapat dikembalikan, hanya jika create binArray baru)_\n"
        else:
            fee_breakdown += f"❌ **Non-Refundable:** 0 SOL\n"
            fee_breakdown += f"   _(Semua binArrays sudah ada)_\n"
        
        preview_embed.add_field(
            name="💵 Fee Breakdown",
            value=fee_breakdown,
            inline=False
        )
        
        preview_embed.set_footer(
            text="⚠️ Pastikan wallet memiliki cukup SOL untuk fees sebelum confirm!"
        )
        
        # Create confirmation buttons
        class ConfirmAddLiquidityView(View):
            def __init__(self, agent, preview_data, slippage_bps):
                super().__init__(timeout=300)  # 5 minutes timeout
                self.agent = agent
                self.preview_data = preview_data
                self.slippage_bps = slippage_bps
                self.confirmed = False
            
            @discord.ui.button(label="✅ Confirm & Add", style=discord.ButtonStyle.green)
            async def confirm_button(self, button_interaction: discord.Interaction, button: Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("❌ Hanya user yang memulai command ini yang bisa confirm!", ephemeral=True)
                    return
                
                await button_interaction.response.defer(ephemeral=True)
                
                # Execute add liquidity
                success, tx_signature, message = await self.agent.add_liquidity(
                    pool_address=self.preview_data['pool_address'],
                    token_x_amount=self.preview_data['token_x_amount'],
                    token_y_amount=self.preview_data['token_y_amount'],
                    token_x_mint=self.preview_data['token_x_mint'],
                    token_y_mint=self.preview_data['token_y_mint'],
                    strategy_type=self.preview_data['strategy_type'],
                    min_price=self.preview_data.get('min_price'),
                    max_price=self.preview_data.get('max_price'),
                    slippage_bps=self.slippage_bps,
                    position_address=self.preview_data.get('position_address'),
                    skip_fee_preview=True
                )
                
                if success and tx_signature:
                    success_embed = discord.Embed(
                        title="✅ Add Liquidity Success",
                        description=f"Liquidity berhasil ditambahkan ke pool",
                        color=0x00ff00
                    )
                    success_embed.add_field(
                        name="📊 Details",
                        value=(
                            f"**Pool:** `{self.preview_data['pool_address'][:8]}...`\n"
                            f"**Token X:** {self.preview_data['token_x_amount']} (`{self.preview_data['token_x_mint'][:8]}...`)\n"
                            f"**Token Y:** {self.preview_data['token_y_amount']} (`{self.preview_data['token_y_mint'][:8]}...`)\n"
                            f"**Strategy:** {self.preview_data['strategy_type'].upper()}\n"
                            f"**Slippage:** {self.slippage_bps / 100}%"
                        ),
                        inline=False
                    )
                    
                    fees = self.preview_data.get('fees', {})
                    if fees.get('total_fee_sol', 0) > 0:
                        success_embed.add_field(
                            name="💵 Fees Paid",
                            value=(
                                f"**Total:** {fees.get('total_fee_sol', 0):.4f} SOL\n"
                                f"✅ Refundable: {fees.get('refundable_fee_sol', 0):.4f} SOL\n"
                                f"❌ Non-Refundable: {fees.get('non_refundable_fee_sol', 0):.4f} SOL"
                            ),
                            inline=False
                        )
                    
                    success_embed.add_field(
                        name="🔗 Transaction",
                        value=f"[View on Solscan](https://solscan.io/tx/{tx_signature})",
                        inline=False
                    )
                    
                    # Disable buttons
                    for item in self.children:
                        item.disabled = True
                    
                    await button_interaction.followup.send(embed=success_embed)
                    await interaction.edit_original_response(embed=preview_embed, view=self)
                else:
                    await button_interaction.followup.send(
                        f"❌ **Add Liquidity Failed**\n\n{message}",
                        ephemeral=True
                    )
            
            @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
            async def cancel_button(self, button_interaction: discord.Interaction, button: Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("❌ Hanya user yang memulai command ini yang bisa cancel!", ephemeral=True)
                    return
                
                await button_interaction.response.defer(ephemeral=True)
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                
                cancel_embed = discord.Embed(
                    title="❌ Cancelled",
                    description="Add liquidity dibatalkan",
                    color=0xff0000
                )
                
                await button_interaction.followup.send(embed=cancel_embed, ephemeral=True)
                await interaction.edit_original_response(embed=preview_embed, view=self)
        
        view = ConfirmAddLiquidityView(agent, preview, slippage_bps)
        await interaction.followup.send(embed=preview_embed, view=view)
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_add: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="lp_remove", description="➖ Remove liquidity dari LP position")
@app_commands.describe(
    position_address="Address LP position",
    pool_address="Address pool Meteora DLMM (required)",
    liquidity_percentage="Percentage liquidity to remove (0-100, default: 100 = all)",
    from_bin_id="Start bin ID untuk remove (opsional)",
    to_bin_id="End bin ID untuk remove (opsional)",
    claim_and_close="Claim fees dan close position setelah remove (default: false)"
)
@app_commands.check(_lp_admin_check)
async def lp_remove(
    interaction: discord.Interaction,
    position_address: str,
    pool_address: str,
    liquidity_percentage: float = 100.0,
    from_bin_id: Optional[int] = None,
    to_bin_id: Optional[int] = None,
    claim_and_close: bool = False
):
    """Remove liquidity dari LP position"""
    if not LP_AGENT_AVAILABLE:
        await interaction.response.send_message("❌ LP Agent tidak tersedia.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        agent = get_lp_agent()
        if not agent or not agent.keypair:
            await interaction.followup.send(
                "❌ LP Agent tidak terinisialisasi. Set LP_WALLET_PRIVATE_KEY environment variable.",
                ephemeral=True
            )
            return
        
        # Validate addresses
        if not is_valid_solana_address(position_address):
            await interaction.followup.send("❌ Invalid position address!", ephemeral=True)
            return
        
        if not is_valid_solana_address(pool_address):
            await interaction.followup.send("❌ Invalid pool address!", ephemeral=True)
            return
        
        # Validate percentage
        if liquidity_percentage < 0 or liquidity_percentage > 100:
            await interaction.followup.send("❌ Liquidity percentage must be between 0 and 100!", ephemeral=True)
            return
        
        # Remove liquidity
        success, tx_signature, message = await agent.remove_liquidity(
            position_address=position_address,
            pool_address=pool_address,
            liquidity_percentage=liquidity_percentage,
            from_bin_id=from_bin_id,
            to_bin_id=to_bin_id,
            should_claim_and_close=claim_and_close
        )
        
        if success and tx_signature:
            embed = discord.Embed(
                title="✅ Remove Liquidity Success",
                description=f"Liquidity berhasil dihapus dari position",
                color=0x00ff00
            )
            embed.add_field(
                name="📊 Details",
                value=(
                    f"**Position:** `{position_address[:8]}...`\n"
                    f"**Percentage:** {liquidity_percentage}%\n"
                    f"**Claim & Close:** {'Yes' if claim_and_close else 'No'}"
                ),
                inline=False
            )
            if from_bin_id is not None and to_bin_id is not None:
                embed.add_field(
                    name="📈 Bin Range",
                    value=f"**From:** {from_bin_id}\n**To:** {to_bin_id}",
                    inline=False
                )
            embed.add_field(
                name="🔗 Transaction",
                value=f"[View on Solscan](https://solscan.io/tx/{tx_signature})",
                inline=False
            )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                f"❌ **Remove Liquidity Failed**\n\n{message}",
                ephemeral=True
            )
        
    except Exception as e:
        print(f"[LP_AGENT] Error in lp_remove: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@lp_pool_info.error
@lp_positions.error
@lp_returns.error
@lp_monitor.error
@lp_add.error
@lp_remove.error
async def lp_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler untuk LP commands"""
    if isinstance(error, app_commands.CheckFailure):
        # Permission check failed
        await interaction.response.send_message(
            f"❌ {str(error)}",
            ephemeral=True
        )
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.",
            ephemeral=True
        )
    else:
        # Only send error if interaction hasn't been responded to
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ Error: {str(error)}",
                ephemeral=True
            )
        else:
            # If already responded, use followup
            await interaction.followup.send(
                f"❌ Error: {str(error)}",
                ephemeral=True
            )

# --- RUN BOT ---
print("[DEBUG] Bot starting...")
bot.run(TOKEN)