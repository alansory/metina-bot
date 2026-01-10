"""
Meteora LP Agent - Automated Liquidity Provider Agent for Meteora DLMM Pools
Menggunakan Solana MCP untuk interaksi blockchain
"""

import os
import json
import base58
import base64
import aiohttp
from typing import Dict, List, Optional, Tuple
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

# Meteora DLMM Program ID
METEORA_DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# Strategy Types untuk Add Liquidity (string values)
# "spot" = SpotBalanced - distribusi merata
# "curve" = Curve - konsentrasi di tengah
# "bid_ask" = BidAsk - konsentrasi di ujung

# Solana MCP Endpoint (default: official Solana MCP server)
SOLANA_MCP_URL = os.getenv("SOLANA_MCP_URL", "https://mcp.solana.com/mcp")

# RPC Configuration
RPC_URL = os.getenv("HELIUS_RPC_URL") or os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"

class MeteoraLPAgent:
    """Agent untuk mengelola posisi LP di Meteora DLMM pools"""
    
    def __init__(self, private_key: Optional[str] = None):
        """
        Initialize Meteora LP Agent
        
        Args:
            private_key: Base58 encoded private key (optional, can be set via env)
        """
        self.private_key = private_key or os.getenv("LP_WALLET_PRIVATE_KEY")
        self.keypair: Optional[Keypair] = None
        self.wallet_address: Optional[str] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        if self.private_key:
            try:
                private_key_bytes = base58.b58decode(self.private_key)
                self.keypair = Keypair.from_bytes(private_key_bytes)
                self.wallet_address = str(self.keypair.pubkey())
                print(f"[LP_AGENT] ✅ Wallet initialized: {self.wallet_address[:8]}...")
            except Exception as e:
                print(f"[LP_AGENT] ❌ Failed to initialize wallet: {e}")
        else:
            print("[LP_AGENT] ⚠️ No private key provided - agent will be read-only")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session"""
        if not self.http_session:
            self.http_session = aiohttp.ClientSession()
        return self.http_session
    
    async def _call_solana_mcp(self, method: str, params: Dict) -> Dict:
        """
        Call Solana MCP server untuk mendapatkan informasi blockchain
        
        Args:
            method: MCP method name
            params: Parameters untuk method
            
        Returns:
            Response dari MCP server
        """
        session = await self._get_session()
        
        try:
            # Solana MCP menggunakan SSE atau HTTP endpoint
            # Untuk sekarang kita gunakan direct RPC call, tapi bisa di-extend untuk MCP
            mcp_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params
            }
            
            async with session.post(
                SOLANA_MCP_URL,
                json=mcp_payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    return await response.json()
                error_text = await response.text()
                print(f"[LP_AGENT] MCP call failed: {response.status} - {error_text}")
                return {"error": {"message": error_text}}
        except Exception as e:
            print(f"[LP_AGENT] Error calling Solana MCP: {e}")
            return {"error": {"message": str(e)}}
    
    async def get_pool_info(self, pool_address: str) -> Optional[Dict]:
        """
        Get informasi pool Meteora DLMM
        
        Args:
            pool_address: Address pool DLMM
            
        Returns:
            Pool information atau None jika error
        """
        session = await self._get_session()
        
        try:
            # Fetch dari Meteora API
            url = "https://dlmm-api.meteora.ag/pair/all_by_groups"
            params = {
                "search_term": pool_address,
                "limit": 1
            }
            
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Extract pool dari response
                    if isinstance(data, dict) and 'groups' in data:
                        for group in data.get('groups', []):
                            for pool in group.get('pairs', []):
                                if pool.get('address', '').lower() == pool_address.lower():
                                    return {
                                        'address': pool.get('address'),
                                        'name': pool.get('name'),
                                        'mint_x': pool.get('mint_x'),
                                        'mint_y': pool.get('mint_y'),
                                        'liquidity': pool.get('liquidity', 0),
                                        'tvl': pool.get('tvl', 0),
                                        'volume_24h': pool.get('volume_24h', 0),
                                        'fees_24h': pool.get('fees_24h', 0),
                                        'bin_step': pool.get('bin_step'),
                                        'base_fee_percentage': pool.get('base_fee_percentage', 0),
                                    }
            
            return None
        except Exception as e:
            print(f"[LP_AGENT] Error fetching pool info: {e}")
            return None
    
    async def get_lp_positions(self, wallet_address: Optional[str] = None) -> List[Dict]:
        """
        Get semua LP positions untuk wallet
        
        Args:
            wallet_address: Wallet address (default: agent's wallet)
            
        Returns:
            List of LP positions
        """
        wallet = wallet_address or self.wallet_address
        
        if not wallet:
            print("[LP_AGENT] No wallet address provided")
            return []
        
        session = await self._get_session()
        
        try:
            # Query program accounts untuk LP positions
            # Meteora DLMM menggunakan position accounts
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getProgramAccounts",
                "params": [
                    METEORA_DLMM_PROGRAM_ID,
                    {
                        "encoding": "jsonParsed",
                        "filters": [
                            {
                                "memcmp": {
                                    "offset": 8,  # Position owner offset
                                    "bytes": wallet
                                }
                            }
                        ]
                    }
                ]
            }
            
            async with session.post(
                RPC_URL,
                json=rpc_payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if "result" in result:
                        positions = []
                        for account in result["result"]:
                            # Parse position data
                            positions.append({
                                'position_address': account.get('pubkey'),
                                'data': account.get('account', {}).get('data', {}),
                            })
                        return positions
                
                return []
        except Exception as e:
            print(f"[LP_AGENT] Error fetching LP positions: {e}")
            return []
    
    def _calculate_bin_id_from_price(self, price: float, bin_step: int) -> int:
        """
        Calculate bin ID dari price menggunakan bin step
        
        Args:
            price: Price ratio
            bin_step: Bin step dari pool
            
        Returns:
            Bin ID
        """
        # Formula: bin_id = log(price) / log(1 + bin_step / 10000)
        import math
        if price <= 0:
            return 0
        bin_step_ratio = bin_step / 10000.0
        bin_id = int(math.log(price) / math.log(1 + bin_step_ratio))
        return bin_id
    
    def _price_to_bin_id(self, price: float, bin_step: int, current_bin_id: int = 0) -> int:
        """
        Convert price to bin ID (simplified calculation)
        
        Args:
            price: Price ratio
            bin_step: Bin step
            current_bin_id: Current active bin ID (optional)
            
        Returns:
            Bin ID
        """
        # Simplified: use relative bin calculation
        # Real implementation would need pool state
        try:
            return self._calculate_bin_id_from_price(price, bin_step)
        except Exception:
            # Fallback: estimate based on current bin
            price_diff = (price - 1.0) * 100  # Percentage difference
            estimated_bins = int(price_diff / (bin_step / 100))
            return current_bin_id + estimated_bins
    
    async def _get_pool_state(self, pool_address: str) -> Optional[Dict]:
        """Get pool state dari on-chain"""
        session = await self._get_session()
        
        try:
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    pool_address,
                    {"encoding": "jsonParsed"}
                ]
            }
            
            async with session.post(
                RPC_URL,
                json=rpc_payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if "result" in result and result["result"]:
                        return result["result"]["value"]
            return None
        except Exception as e:
            print(f"[LP_AGENT] Error fetching pool state: {e}")
            return None
    
    async def _get_existing_binarrays(self, pool_address: str, min_bin_id: int, max_bin_id: int) -> set:
        """
        Get existing binArray accounts untuk bin range tertentu
        Menggunakan Meteora SDK atau query on-chain
        
        Args:
            pool_address: Pool address
            min_bin_id: Minimum bin ID
            max_bin_id: Maximum bin ID
            
        Returns:
            Set of binArray indices yang sudah ada (empty set jika tidak bisa check)
        """
        session = await self._get_session()
        existing_binarrays = set()
        
        try:
            # Try to use Meteora SDK wrapper untuk get binArrays
            try:
                from meteora_sdk_wrapper import get_sdk_wrapper
                sdk_wrapper = get_sdk_wrapper(RPC_URL)
                
                if sdk_wrapper.sdk_installed:
                    # Use SDK to get binArrays
                    # Meteora SDK has method to get binArrays from pool
                    # For now, we'll use a simplified approach
                    # Full implementation would call SDK method to get all binArrays
                    pass
            except ImportError:
                pass
            
            # Query on-chain untuk binArray accounts
            # Meteora DLMM binArray accounts are PDAs derived from:
            # - Pool address
            # - BinArray index (binArray index = floor(bin_id / 70))
            # 
            # Formula untuk derive binArray PDA:
            # seeds = [b"binArray", pool_address, binArray_index]
            # 
            # We need to check which binArrays exist on-chain
            # Calculate binArray indices needed
            BINS_PER_BINARRAY = 70
            min_binarray_idx = min_bin_id // BINS_PER_BINARRAY
            max_binarray_idx = max_bin_id // BINS_PER_BINARRAY
            
            # Try to query program accounts untuk binArrays
            # This requires deriving PDA addresses and checking if they exist
            # For now, return empty set (will use conservative estimate)
            # 
            # Note: Full implementation would:
            # 1. Derive binArray PDA addresses using anchor IDL
            # 2. Use getMultipleAccounts to check which exist
            # 3. Return set of existing binArray indices
            
            # Alternative: Use Meteora API if available
            # Some Meteora APIs might provide binArray info
            
        except Exception as e:
            print(f"[LP_AGENT] Error checking existing binArrays: {e}")
        
        return existing_binarrays
    
    async def estimate_add_liquidity_fees(
        self,
        pool_address: str,
        min_bin_id: int,
        max_bin_id: int,
        position_address: Optional[str] = None
    ) -> Dict:
        """
        Estimate fees untuk add liquidity (refundable dan non-refundable)
        Menggunakan cara Meteora sesuai dokumentasi resmi
        
        Args:
            pool_address: Pool address
            min_bin_id: Minimum bin ID
            max_bin_id: Maximum bin ID
            position_address: Existing position (None untuk new position)
            
        Returns:
            Dict dengan fee breakdown:
            {
                'refundable_fee_sol': float,
                'non_refundable_fee_sol': float,
                'total_fee_sol': float,
                'position_rent_sol': float,
                'extension_rent_sol': float,
                'binarray_rent_sol': float,
                'num_bins': int,
                'needs_extension': bool,
                'needs_new_binarrays': int,
                'existing_binarrays': int,
                'total_binarrays_needed': int
            }
        
        Note:
            Estimate fees sesuai dokumentasi resmi Meteora:
            https://docs.meteora.ag/user-guide/usage/getting-started
            
            Fee Structure:
            - Refundable: Dapat dikembalikan saat close position
              * Position Rent: ~0.059 SOL (untuk new position)
              * Extension Rent: ~0.002 SOL per extension (jika > 69 bins)
            - Non-Refundable: Tidak dapat dikembalikan
              * BinArray Creation: ~0.075 SOL per binArray (hanya jika create binArray baru)
        """
        # Constants dari Meteora Documentation
        # Source: https://docs.meteora.ag/user-guide/usage/getting-started
        POSITION_RENT_SOL = 0.059  # ~0.059 SOL per position (refundable)
        BINARRAY_RENT_SOL = 0.075  # ~0.075 SOL per binArray (non-refundable)
        MAX_BINS_WITHOUT_EXTENSION = 69  # Extension needed if > 69 bins
        EXTENSION_RENT_SOL = 0.002  # ~0.002 SOL per extension (refundable)
        BINS_PER_BINARRAY = 70  # Each binArray holds 70 bins
        
        num_bins = max_bin_id - min_bin_id + 1
        needs_extension = num_bins > MAX_BINS_WITHOUT_EXTENSION
        
        # Refundable fees
        position_rent_sol = 0.0
        extension_rent_sol = 0.0
        
        # Non-refundable fees
        binarray_rent_sol = 0.0
        
        # Position rent (only for new positions)
        # Refundable saat close position
        if not position_address:
            position_rent_sol = POSITION_RENT_SOL
        
        # Extension rent (if needed)
        # Refundable saat close position
        if needs_extension:
            # Calculate number of extensions needed
            # Each position can hold 69 bins without extension
            # Each extension adds capacity for more bins
            # Formula: extensions needed = ceil((num_bins - 69) / 70)
            bins_over_limit = num_bins - MAX_BINS_WITHOUT_EXTENSION
            extensions_needed = (bins_over_limit + BINS_PER_BINARRAY - 1) // BINS_PER_BINARRAY
            extension_rent_sol = extensions_needed * EXTENSION_RENT_SOL
        
        # BinArray rent (non-refundable)
        # Only charged if creating NEW binArrays (binArrays that don't exist yet)
        # Each binArray covers 70 bins
        # Formula: binArray index = floor(bin_id / 70)
        min_binarray_idx = min_bin_id // BINS_PER_BINARRAY
        max_binarray_idx = max_bin_id // BINS_PER_BINARRAY
        total_binarrays_needed = max_binarray_idx - min_binarray_idx + 1
        
        # Try to check existing binArrays on-chain
        existing_binarrays = await self._get_existing_binarrays(pool_address, min_bin_id, max_bin_id)
        
        # Calculate how many NEW binArrays need to be created
        if existing_binarrays:
            # We have info about existing binArrays
            new_binarrays_needed = 0
            for idx in range(min_binarray_idx, max_binarray_idx + 1):
                if idx not in existing_binarrays:
                    new_binarrays_needed += 1
        else:
            # No info about existing binArrays - use conservative estimate
            # Most pools already have binArrays for common price ranges
            # Estimate: 0-1 new binArrays (conservative)
            # In reality, most binArrays already exist for active pools
            new_binarrays_needed = min(1, total_binarrays_needed)
        
        binarray_rent_sol = new_binarrays_needed * BINARRAY_RENT_SOL
        
        # Calculate totals
        refundable_fee_sol = position_rent_sol + extension_rent_sol
        non_refundable_fee_sol = binarray_rent_sol
        total_fee_sol = refundable_fee_sol + non_refundable_fee_sol
        
        return {
            'refundable_fee_sol': refundable_fee_sol,
            'non_refundable_fee_sol': non_refundable_fee_sol,
            'total_fee_sol': total_fee_sol,
            'position_rent_sol': position_rent_sol,
            'extension_rent_sol': extension_rent_sol,
            'binarray_rent_sol': binarray_rent_sol,
            'num_bins': num_bins,
            'needs_extension': needs_extension,
            'estimated_new_binarrays': new_binarrays_needed,
            'existing_binarrays': len(existing_binarrays) if existing_binarrays else None,
            'total_binarrays_needed': total_binarrays_needed
        }
    
    async def preview_add_liquidity(
        self,
        pool_address: str,
        token_x_amount: float,
        token_y_amount: float,
        token_x_mint: str,
        token_y_mint: str,
        strategy_type: str = "spot",
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        position_address: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Preview add liquidity dengan fee breakdown (tanpa execute transaction)
        
        Returns:
            Dict dengan preview info termasuk fees
        """
        try:
            # Get pool info
            pool_info = await self.get_pool_info(pool_address)
            if not pool_info:
                return None
            
            bin_step = pool_info.get('bin_step', 100)
            pool_state = await self._get_pool_state(pool_address)
            current_bin_id = 0
            
            # Calculate bin IDs
            if min_price and max_price:
                min_bin_id = self._price_to_bin_id(min_price, bin_step, current_bin_id)
                max_bin_id = self._price_to_bin_id(max_price, bin_step, current_bin_id)
            else:
                min_bin_id = current_bin_id - 20
                max_bin_id = current_bin_id + 20
            
            # Estimate fees
            fee_info = await self.estimate_add_liquidity_fees(
                pool_address,
                min_bin_id,
                max_bin_id,
                position_address
            )
            
            return {
                'pool_address': pool_address,
                'pool_name': pool_info.get('name', 'Unknown'),
                'token_x_amount': token_x_amount,
                'token_y_amount': token_y_amount,
                'token_x_mint': token_x_mint,
                'token_y_mint': token_y_mint,
                'strategy_type': strategy_type,
                'min_bin_id': min_bin_id,
                'max_bin_id': max_bin_id,
                'min_price': min_price,
                'max_price': max_price,
                'position_address': position_address,
                'fees': fee_info
            }
        except Exception as e:
            print(f"[LP_AGENT] Error in preview_add_liquidity: {e}")
            return None
    
    async def add_liquidity(
        self,
        pool_address: str,
        token_x_amount: float,
        token_y_amount: float,
        token_x_mint: str,
        token_y_mint: str,
        strategy_type: str = "spot",  # spot, curve, bid_ask
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        slippage_bps: int = 100,  # 1% slippage
        position_address: Optional[str] = None,  # Existing position or None for new
        use_sdk: bool = True,  # Use Meteora SDK if available
        skip_fee_preview: bool = False  # Skip fee preview (for internal use)
    ) -> Tuple[bool, Optional[str], str]:
        """
        Add liquidity ke Meteora DLMM pool
        
        Args:
            pool_address: Address pool DLMM
            token_x_amount: Amount token X (dalam token units, not lamports)
            token_y_amount: Amount token Y (dalam token units, not lamports)
            token_x_mint: Mint address untuk token X
            token_y_mint: Mint address untuk token Y
            strategy_type: Strategy type - "spot", "curve", or "bid_ask"
            min_price: Minimum price untuk price range
            max_price: Maximum price untuk price range
            slippage_bps: Slippage tolerance in basis points (100 = 1%)
            position_address: Existing position address (None untuk create new)
            
        Returns:
            (success, tx_signature, message)
        """
        if not self.keypair:
            return False, None, "Wallet not initialized"
        
        session = await self._get_session()
        
        try:
            # Get pool info
            pool_info = await self.get_pool_info(pool_address)
            if not pool_info:
                return False, None, f"Pool not found: {pool_address}"
            
            bin_step = pool_info.get('bin_step', 100)
            
            # Get pool state untuk current bin
            pool_state = await self._get_pool_state(pool_address)
            current_bin_id = 0
            if pool_state and 'data' in pool_state:
                # Parse current bin from pool state (simplified)
                # Real implementation would parse the account data properly
                pass
            
            # Calculate bin IDs dari price range
            if min_price and max_price:
                min_bin_id = self._price_to_bin_id(min_price, bin_step, current_bin_id)
                max_bin_id = self._price_to_bin_id(max_price, bin_step, current_bin_id)
            else:
                # Default: use current bin ± 20 bins
                min_bin_id = current_bin_id - 20
                max_bin_id = current_bin_id + 20
            
            # Convert amounts to lamports (assuming 9 decimals for most tokens)
            # In real implementation, need to fetch token decimals
            token_x_decimals = 9  # Default, should fetch from token
            token_y_decimals = 9  # Default, should fetch from token
            
            token_x_lamports = int(token_x_amount * (10 ** token_x_decimals))
            token_y_lamports = int(token_y_amount * (10 ** token_y_decimals))
            
            # Try to use Meteora SDK wrapper first (if available)
            if use_sdk:
                try:
                    from meteora_sdk_wrapper import get_sdk_wrapper
                    sdk_wrapper = get_sdk_wrapper(RPC_URL)
                    
                    # Convert amounts to lamports (assuming 9 decimals, should fetch from token)
                    token_x_decimals = 9
                    token_y_decimals = 9
                    token_x_lamports = int(token_x_amount * (10 ** token_x_decimals))
                    token_y_lamports = int(token_y_amount * (10 ** token_y_decimals))
                    
                    # Calculate bin IDs from prices if provided
                    min_bin_id = None
                    max_bin_id = None
                    if min_price and max_price:
                        min_bin_id = self._price_to_bin_id(min_price, bin_step, current_bin_id)
                        max_bin_id = self._price_to_bin_id(max_price, bin_step, current_bin_id)
                    
                    # Use SDK wrapper
                    success, tx_base64, error_msg = await sdk_wrapper.add_liquidity(
                        pool_address=pool_address,
                        user_wallet=self.wallet_address,
                        token_x_amount=token_x_lamports,
                        token_y_amount=token_y_lamports,
                        token_x_mint=token_x_mint,
                        token_y_mint=token_y_mint,
                        strategy_type=strategy_type,
                        min_bin_id=min_bin_id,
                        max_bin_id=max_bin_id,
                        slippage_bps=slippage_bps,
                        position_address=position_address
                    )
                    
                    if success and tx_base64:
                        # Decode and sign transaction
                        tx_bytes = base64.b64decode(tx_base64)
                        transaction = VersionedTransaction.from_bytes(tx_bytes)
                        
                        # Sign
                        signed_tx = VersionedTransaction(transaction.message, [self.keypair])
                        
                        # Send
                        tx_base64_signed = base64.b64encode(bytes(signed_tx)).decode('utf-8')
                        
                        rpc_payload = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                tx_base64_signed,
                                {
                                    "encoding": "base64",
                                    "skipPreflight": False,
                                    "preflightCommitment": "confirmed",
                                    "maxRetries": 3,
                                }
                            ]
                        }
                        
                        async with session.post(
                            RPC_URL,
                            json=rpc_payload,
                            timeout=aiohttp.ClientTimeout(total=60)
                        ) as rpc_response:
                            result = await rpc_response.json()
                            
                            if "error" in result:
                                return False, None, f"Transaction failed: {result['error']}"
                            
                            signature = result.get("result")
                            if signature:
                                print(f"[LP_AGENT] ✅ Add liquidity transaction sent: {signature}")
                                return True, signature, f"Transaction sent: {signature}"
                            
                            return False, None, "No signature in response"
                    elif error_msg and "not installed" not in error_msg.lower():
                        # SDK error but not installation error
                        return False, None, f"SDK error: {error_msg}"
                except ImportError:
                    print("[LP_AGENT] Meteora SDK wrapper not available, trying API...")
                except Exception as sdk_error:
                    print(f"[LP_AGENT] SDK wrapper error: {sdk_error}, trying API...")
            
            # Fallback: Try to use Meteora API untuk build transaction
            # Meteora mungkin punya API endpoint untuk build add liquidity transaction
            meteora_api_url = "https://dlmm-api.meteora.ag/transaction/add-liquidity"
            
            payload = {
                "pool": pool_address,
                "user": self.wallet_address,
                "tokenXAmount": str(token_x_lamports),
                "tokenYAmount": str(token_y_lamports),
                "tokenXMint": token_x_mint,
                "tokenYMint": token_y_mint,
                "strategy": {
                    "type": strategy_type,
                    "minBinId": min_bin_id,
                    "maxBinId": max_bin_id
                },
                "slippageBps": slippage_bps,
                "position": position_address  # None untuk new position
            }
            
            try:
                async with session.post(
                    meteora_api_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        tx_data = await response.json()
                        transaction_base64 = tx_data.get("transaction")
                        
                        if transaction_base64:
                            # Decode and sign transaction
                            tx_bytes = base64.b64decode(transaction_base64)
                            transaction = VersionedTransaction.from_bytes(tx_bytes)
                            
                            # Sign
                            signed_tx = VersionedTransaction(transaction.message, [self.keypair])
                            
                            # Send
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
                            
                            async with session.post(
                                RPC_URL,
                                json=rpc_payload,
                                timeout=aiohttp.ClientTimeout(total=60)
                            ) as rpc_response:
                                result = await rpc_response.json()
                                
                                if "error" in result:
                                    return False, None, f"Transaction failed: {result['error']}"
                                
                                signature = result.get("result")
                                if signature:
                                    print(f"[LP_AGENT] ✅ Add liquidity transaction sent: {signature}")
                                    return True, signature, f"Transaction sent: {signature}"
                                
                                return False, None, "No signature in response"
            except Exception as api_error:
                print(f"[LP_AGENT] Meteora API not available, trying manual build: {api_error}")
                # Fallback: manual transaction building would go here
                # This requires Meteora program instruction encoding
                # For now, return error message
            
            # If API not available, return error with instructions
            return False, None, (
                "Meteora API untuk build transaction tidak tersedia. "
                "Untuk implementasi lengkap, diperlukan Meteora SDK atau manual instruction encoding. "
                f"Parameters: pool={pool_address}, strategy={strategy_type}, "
                f"min_bin={min_bin_id}, max_bin={max_bin_id}"
            )
            
        except Exception as e:
            print(f"[LP_AGENT] Error adding liquidity: {e}")
            import traceback
            traceback.print_exc()
            return False, None, f"Error: {str(e)}"
    
    async def remove_liquidity(
        self,
        position_address: str,
        liquidity_percentage: float = 100.0,  # Percentage to remove (100 = all)
        from_bin_id: Optional[int] = None,
        to_bin_id: Optional[int] = None,
        should_claim_and_close: bool = False,
        pool_address: Optional[str] = None,  # Pool address (optional, will try to fetch from position)
        use_sdk: bool = True  # Use Meteora SDK if available
    ) -> Tuple[bool, Optional[str], str]:
        """
        Remove liquidity dari position
        
        Args:
            position_address: Address LP position
            liquidity_percentage: Percentage liquidity to remove (0-100, default: 100 = all)
            from_bin_id: Start bin ID untuk remove (None = all bins)
            to_bin_id: End bin ID untuk remove (None = all bins)
            should_claim_and_close: Claim fees dan close position setelah remove
            
        Returns:
            (success, tx_signature, message)
        """
        if not self.keypair:
            return False, None, "Wallet not initialized"
        
        session = await self._get_session()
        
        try:
            # Validate percentage
            if liquidity_percentage < 0 or liquidity_percentage > 100:
                return False, None, "Liquidity percentage must be between 0 and 100"
            
            # Convert percentage to basis points (10000 = 100%)
            bps = int(liquidity_percentage * 100)
            
            # Get position info untuk validasi
            position_data = await self.monitor_position(position_address)
            if not position_data:
                return False, None, f"Position not found: {position_address}"
            
            # Try to get pool address from position if not provided
            # In real implementation, would parse position account data
            if not pool_address:
                # Try to fetch from position account data
                # For now, require pool_address parameter
                return False, None, "pool_address required for remove_liquidity. Please provide pool address."
            
            # Try to use Meteora SDK wrapper first (if available)
            if use_sdk:
                try:
                    from meteora_sdk_wrapper import get_sdk_wrapper
                    sdk_wrapper = get_sdk_wrapper(RPC_URL)
                    
                    # Use SDK wrapper
                    success, tx_base64, error_msg = await sdk_wrapper.remove_liquidity(
                        pool_address=pool_address,
                        user_wallet=self.wallet_address,
                        position_address=position_address,
                        bps=bps,
                        from_bin_id=from_bin_id,
                        to_bin_id=to_bin_id,
                        should_claim_and_close=should_claim_and_close
                    )
                    
                    if success and tx_base64:
                        # Decode and sign transaction
                        tx_bytes = base64.b64decode(tx_base64)
                        transaction = VersionedTransaction.from_bytes(tx_bytes)
                        
                        # Sign
                        signed_tx = VersionedTransaction(transaction.message, [self.keypair])
                        
                        # Send
                        tx_base64_signed = base64.b64encode(bytes(signed_tx)).decode('utf-8')
                        
                        rpc_payload = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                tx_base64_signed,
                                {
                                    "encoding": "base64",
                                    "skipPreflight": False,
                                    "preflightCommitment": "confirmed",
                                    "maxRetries": 3,
                                }
                            ]
                        }
                        
                        async with session.post(
                            RPC_URL,
                            json=rpc_payload,
                            timeout=aiohttp.ClientTimeout(total=60)
                        ) as rpc_response:
                            result = await rpc_response.json()
                            
                            if "error" in result:
                                return False, None, f"Transaction failed: {result['error']}"
                            
                            signature = result.get("result")
                            if signature:
                                print(f"[LP_AGENT] ✅ Remove liquidity transaction sent: {signature}")
                                return True, signature, f"Transaction sent: {signature}"
                            
                            return False, None, "No signature in response"
                    elif error_msg and "not installed" not in error_msg.lower():
                        # SDK error but not installation error
                        return False, None, f"SDK error: {error_msg}"
                except ImportError:
                    print("[LP_AGENT] Meteora SDK wrapper not available, trying API...")
                except Exception as sdk_error:
                    print(f"[LP_AGENT] SDK wrapper error: {sdk_error}, trying API...")
            
            # Fallback: Try to use Meteora API untuk build transaction
            meteora_api_url = "https://dlmm-api.meteora.ag/transaction/remove-liquidity"
            
            payload = {
                "user": self.wallet_address,
                "position": position_address,
                "bps": bps,
                "fromBinId": from_bin_id,
                "toBinId": to_bin_id,
                "shouldClaimAndClose": should_claim_and_close
            }
            
            try:
                async with session.post(
                    meteora_api_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        tx_data = await response.json()
                        transaction_base64 = tx_data.get("transaction")
                        
                        if transaction_base64:
                            # Decode and sign transaction
                            tx_bytes = base64.b64decode(transaction_base64)
                            transaction = VersionedTransaction.from_bytes(tx_bytes)
                            
                            # Sign
                            signed_tx = VersionedTransaction(transaction.message, [self.keypair])
                            
                            # Send
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
                            
                            async with session.post(
                                RPC_URL,
                                json=rpc_payload,
                                timeout=aiohttp.ClientTimeout(total=60)
                            ) as rpc_response:
                                result = await rpc_response.json()
                                
                                if "error" in result:
                                    return False, None, f"Transaction failed: {result['error']}"
                                
                                signature = result.get("result")
                                if signature:
                                    print(f"[LP_AGENT] ✅ Remove liquidity transaction sent: {signature}")
                                    return True, signature, f"Transaction sent: {signature}"
                                
                                return False, None, "No signature in response"
            except Exception as api_error:
                print(f"[LP_AGENT] Meteora API not available, trying manual build: {api_error}")
                # Fallback: manual transaction building would go here
                # For now, return error message
            
            # If API not available, return error with instructions
            return False, None, (
                "Meteora API untuk build transaction tidak tersedia. "
                "Untuk implementasi lengkap, diperlukan Meteora SDK atau manual instruction encoding. "
                f"Parameters: position={position_address}, bps={bps}, "
                f"from_bin={from_bin_id}, to_bin={to_bin_id}, claim_and_close={should_claim_and_close}"
            )
            
        except Exception as e:
            print(f"[LP_AGENT] Error removing liquidity: {e}")
            import traceback
            traceback.print_exc()
            return False, None, f"Error: {str(e)}"
    
    async def calculate_lp_returns(
        self,
        pool_address: str,
        token_x_amount: float,
        token_y_amount: float,
        days: int = 7
    ) -> Optional[Dict]:
        """
        Calculate estimated LP returns berdasarkan historical data
        
        Args:
            pool_address: Pool address
            token_x_amount: Amount token X
            token_y_amount: Amount token Y
            days: Number of days untuk projection
            
        Returns:
            Estimated returns data
        """
        pool_info = await self.get_pool_info(pool_address)
        if not pool_info:
            return None
        
        try:
            # Calculate based on fees and volume
            fees_24h = pool_info.get('fees_24h', 0)
            tvl = pool_info.get('tvl', 0)
            
            if tvl <= 0:
                return None
            
            # Estimate user's share of pool
            user_tvl = (token_x_amount + token_y_amount)  # Simplified
            pool_share = user_tvl / tvl if tvl > 0 else 0
            
            # Estimate daily returns
            daily_fees_share = fees_24h * pool_share
            estimated_7d_returns = daily_fees_share * days
            
            # Calculate APR
            if user_tvl > 0:
                apr = (daily_fees_share * 365 / user_tvl) * 100
            else:
                apr = 0
            
            return {
                'pool_address': pool_address,
                'user_tvl': user_tvl,
                'pool_share_pct': pool_share * 100,
                'daily_fees_estimate': daily_fees_share,
                'estimated_7d_returns': estimated_7d_returns,
                'estimated_apr': apr,
                'pool_tvl': tvl,
                'pool_fees_24h': fees_24h
            }
        except Exception as e:
            print(f"[LP_AGENT] Error calculating LP returns: {e}")
            return None
    
    async def monitor_position(self, position_address: str) -> Optional[Dict]:
        """
        Monitor LP position dan return current status
        
        Args:
            position_address: LP position address
            
        Returns:
            Position status data
        """
        session = await self._get_session()
        
        try:
            # Get position account data dari blockchain
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    position_address,
                    {"encoding": "jsonParsed"}
                ]
            }
            
            async with session.post(
                RPC_URL,
                json=rpc_payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if "result" in result and result["result"]:
                        account_data = result["result"]["value"]
                        return {
                            'position_address': position_address,
                            'lamports': account_data.get('lamports', 0),
                            'data': account_data.get('data', {}),
                            'owner': account_data.get('owner'),
                        }
            
            return None
        except Exception as e:
            print(f"[LP_AGENT] Error monitoring position: {e}")
            return None
    
    async def close(self):
        """Close HTTP session"""
        if self.http_session:
            await self.http_session.close()
            self.http_session = None

# Global agent instance
_lp_agent: Optional[MeteoraLPAgent] = None

def get_lp_agent() -> Optional[MeteoraLPAgent]:
    """Get global LP agent instance"""
    global _lp_agent
    if _lp_agent is None:
        _lp_agent = MeteoraLPAgent()
    return _lp_agent

