"""
Meteora SDK Wrapper - Wrapper untuk menggunakan Meteora TypeScript SDK dari Python
Menggunakan Node.js subprocess untuk execute Meteora SDK
"""

import os
import json
import subprocess
import tempfile
from typing import Dict, Optional, Tuple
import base64

# Path ke Node.js (default: assume in PATH)
NODE_PATH = os.getenv("NODE_PATH", "node")
NPM_PATH = os.getenv("NPM_PATH", "npm")

class MeteoraSDKWrapper:
    """Wrapper untuk Meteora TypeScript SDK via Node.js"""
    
    def __init__(self, rpc_url: str = "https://api.mainnet-beta.solana.com"):
        """
        Initialize wrapper
        
        Args:
            rpc_url: Solana RPC URL
        """
        self.rpc_url = rpc_url
        self.sdk_installed = self._check_sdk_installed()
        
        if not self.sdk_installed:
            print("[METEORA_SDK] ⚠️ Meteora SDK not installed. Run: npm install @meteora-ag/dlmm @solana/web3.js")
    
    def _check_sdk_installed(self) -> bool:
        """Check if Meteora SDK is installed"""
        try:
            # Check if node_modules exists or try to require
            result = subprocess.run(
                [NODE_PATH, "-e", "require('@meteora-ag/dlmm')"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _run_node_script(self, script: str) -> Tuple[bool, Optional[Dict], str]:
        """
        Run Node.js script dan return result
        
        Args:
            script: JavaScript code to execute
            
        Returns:
            (success, result_dict, error_message)
        """
        try:
            # Create temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                f.write(script)
                temp_file = f.name
            
            try:
                # Run script
                result = subprocess.run(
                    [NODE_PATH, temp_file],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if result.returncode == 0:
                    try:
                        output = result.stdout.strip()
                        if output:
                            data = json.loads(output)
                            return True, data, ""
                        return False, None, "No output from script"
                    except json.JSONDecodeError:
                        return False, None, f"Invalid JSON output: {result.stdout}"
                else:
                    error_msg = result.stderr or result.stdout
                    return False, None, f"Script error: {error_msg}"
            finally:
                # Cleanup
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass
                    
        except subprocess.TimeoutExpired:
            return False, None, "Script execution timeout"
        except Exception as e:
            return False, None, f"Error running script: {str(e)}"
    
    async def add_liquidity(
        self,
        pool_address: str,
        user_wallet: str,
        token_x_amount: int,  # in lamports
        token_y_amount: int,  # in lamports
        token_x_mint: str,
        token_y_mint: str,
        strategy_type: str = "spot",
        min_bin_id: Optional[int] = None,
        max_bin_id: Optional[int] = None,
        slippage_bps: int = 100,
        position_address: Optional[str] = None
    ) -> Tuple[bool, Optional[str], str]:
        """
        Add liquidity menggunakan Meteora SDK
        
        Args:
            pool_address: Pool address
            user_wallet: User wallet address
            token_x_amount: Token X amount in lamports
            token_y_amount: Token Y amount in lamports
            token_x_mint: Token X mint
            token_y_mint: Token Y mint
            strategy_type: Strategy type (spot, curve, bid_ask)
            min_bin_id: Minimum bin ID
            max_bin_id: Maximum bin ID
            slippage_bps: Slippage in basis points
            position_address: Existing position address (optional)
            
        Returns:
            (success, transaction_base64, error_message)
        """
        if not self.sdk_installed:
            return False, None, "Meteora SDK not installed. Install with: npm install @meteora-ag/dlmm @solana/web3.js"
        
        # Map strategy type
        strategy_map = {
            "spot": "StrategyType.SpotBalanced",
            "curve": "StrategyType.Curve",
            "bid_ask": "StrategyType.BidAsk"
        }
        
        strategy_enum = strategy_map.get(strategy_type.lower(), "StrategyType.SpotBalanced")
        
        script = f"""
const {{ Connection, PublicKey }} = require('@solana/web3.js');
const DLMM = require('@meteora-ag/dlmm');
const {{ BN }} = require('bn.js');

async function main() {{
    try {{
        const connection = new Connection('{self.rpc_url}', 'confirmed');
        const poolAddress = new PublicKey('{pool_address}');
        const userPublicKey = new PublicKey('{user_wallet}');
        
        // Create DLMM instance
        const dlmmPool = await DLMM.create(connection, poolAddress);
        
        // Calculate bin IDs if not provided
        let minBinId = {min_bin_id if min_bin_id is not None else 'null'};
        let maxBinId = {max_bin_id if max_bin_id is not None else 'null'};
        
        // If bin IDs not provided, use current active bin ± 20
        if (minBinId === null || maxBinId === null) {{
            const activeBinId = dlmmPool.lbPair.activeId;
            minBinId = minBinId !== null ? minBinId : activeBinId - 20;
            maxBinId = maxBinId !== null ? maxBinId : activeBinId + 20;
        }}
        
        // Strategy type
        const StrategyType = DLMM.StrategyType;
        const strategy = {{
            minBinId: minBinId,
            maxBinId: maxBinId,
            strategyType: {strategy_enum}
        }};
        
        // Add liquidity
        const addLiquidityParams = {{
            totalXAmount: new BN({token_x_amount}),
            totalYAmount: new BN({token_y_amount}),
            strategy: strategy,
            user: userPublicKey,
            slippage: {slippage_bps / 100}
        }};
        
        {f"addLiquidityParams.positionPubKey = new PublicKey('{position_address}');" if position_address else ""}
        
        const transaction = await dlmmPool.addLiquidityByStrategy(addLiquidityParams);
        
        // Serialize transaction
        const serialized = transaction.serialize({{
            requireAllSignatures: false,
            verifySignatures: false
        }});
        
        const base64 = Buffer.from(serialized).toString('base64');
        console.log(JSON.stringify({{ success: true, transaction: base64 }}));
    }} catch (error) {{
        console.log(JSON.stringify({{ success: false, error: error.message }}));
        process.exit(1);
    }}
}}

main();
"""
        
        success, result, error = self._run_node_script(script)
        
        if success and result:
            if result.get("success") and result.get("transaction"):
                return True, result["transaction"], ""
            else:
                return False, None, result.get("error", "Unknown error")
        else:
            return False, None, error
    
    async def remove_liquidity(
        self,
        pool_address: str,
        user_wallet: str,
        position_address: str,
        bps: int = 10000,  # 10000 = 100%
        from_bin_id: Optional[int] = None,
        to_bin_id: Optional[int] = None,
        should_claim_and_close: bool = False
    ) -> Tuple[bool, Optional[str], str]:
        """
        Remove liquidity menggunakan Meteora SDK
        
        Args:
            pool_address: Pool address
            user_wallet: User wallet address
            position_address: Position address
            bps: Basis points (10000 = 100%)
            from_bin_id: From bin ID (optional)
            to_bin_id: To bin ID (optional)
            should_claim_and_close: Claim and close position
            
        Returns:
            (success, transaction_base64, error_message)
        """
        if not self.sdk_installed:
            return False, None, "Meteora SDK not installed. Install with: npm install @meteora-ag/dlmm @solana/web3.js"
        
        script = f"""
const {{ Connection, PublicKey }} = require('@solana/web3.js');
const DLMM = require('@meteora-ag/dlmm');

async function main() {{
    try {{
        const connection = new Connection('{self.rpc_url}', 'confirmed');
        const poolAddress = new PublicKey('{pool_address}');
        const userPublicKey = new PublicKey('{user_wallet}');
        const positionPubKey = new PublicKey('{position_address}');
        
        // Create DLMM instance
        const dlmmPool = await DLMM.create(connection, poolAddress);
        
        // Remove liquidity params
        const removeParams = {{
            user: userPublicKey,
            position: positionPubKey,
            bps: {bps},
            shouldClaimAndClose: {str(should_claim_and_close).lower()}
        }};
        
        {f"removeParams.fromBinId = {from_bin_id};" if from_bin_id is not None else ""}
        {f"removeParams.toBinId = {to_bin_id};" if to_bin_id is not None else ""}
        
        const transaction = await dlmmPool.removeLiquidity(removeParams);
        
        // Serialize transaction
        const serialized = transaction.serialize({{
            requireAllSignatures: false,
            verifySignatures: false
        }});
        
        const base64 = Buffer.from(serialized).toString('base64');
        console.log(JSON.stringify({{ success: true, transaction: base64 }}));
    }} catch (error) {{
        console.log(JSON.stringify({{ success: false, error: error.message }}));
        process.exit(1);
    }}
}}

main();
"""
        
        success, result, error = self._run_node_script(script)
        
        if success and result:
            if result.get("success") and result.get("transaction"):
                return True, result["transaction"], ""
            else:
                return False, None, result.get("error", "Unknown error")
        else:
            return False, None, error

# Global wrapper instance
_sdk_wrapper: Optional[MeteoraSDKWrapper] = None

def get_sdk_wrapper(rpc_url: Optional[str] = None) -> MeteoraSDKWrapper:
    """Get global SDK wrapper instance"""
    global _sdk_wrapper
    if _sdk_wrapper is None:
        _sdk_wrapper = MeteoraSDKWrapper(rpc_url or os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"))
    return _sdk_wrapper

