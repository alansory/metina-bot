"""
Config dari env untuk meme-lp-autopilot.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# RPC
RPC_URL = os.getenv("HELIUS_RPC_URL") or os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"

# Wallet (opsional; kosong = monitor only)
LP_WALLET_PRIVATE_KEY = os.getenv("LP_WALLET_PRIVATE_KEY", "").strip()

# Target profit % (1–5)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "2.0"))

# Interval cek (detik)
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))

# Filter pool
MIN_POOL_TVL_USD = float(os.getenv("MIN_POOL_TVL_USD", "500"))

# File posisi
POSITIONS_FILE = os.getenv("POSITIONS_FILE", "positions.json")
POSITIONS_PATH = Path(POSITIONS_FILE)

# Meteora API
METEORA_DLMM_API = "https://dlmm-api.meteora.ag/pair/all_by_groups"
