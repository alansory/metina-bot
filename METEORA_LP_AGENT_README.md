# Meteora LP Agent - Solana MCP Integration

Agent untuk mengelola posisi Liquidity Provider (LP) di Meteora DLMM pools menggunakan Solana MCP.

## Fitur

- 📊 **Pool Information**: Get informasi detail tentang pool Meteora DLMM
- 📈 **LP Returns Calculator**: Calculate estimated returns untuk posisi LP
- 👁️ **Position Monitoring**: Monitor status LP positions
- 📋 **Position List**: Lihat semua LP positions untuk wallet

## Setup

### 1. Environment Variables

Tambahkan ke environment variables:

```bash
# Optional: Wallet private key untuk LP operations (base58 encoded)
LP_WALLET_PRIVATE_KEY=your_base58_private_key_here

# Optional: Solana MCP URL (default: https://mcp.solana.com/mcp)
SOLANA_MCP_URL=https://mcp.solana.com/mcp

# RPC URL (gunakan HELIUS_RPC_URL atau RPC_URL)
HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### 2. Dependencies

Dependencies sudah termasuk di `requirements.txt`:
- `solders` - Solana SDK
- `base58` - Base58 encoding
- `aiohttp` - Async HTTP client

## Discord Commands

**⚠️ Semua LP Agent commands hanya bisa digunakan oleh Admin/Moderator**

Permission yang diperlukan:
- Administrator
- Manage Guild
- Manage Channels
- Manage Messages

### `/lp_pool_info`
Get informasi pool Meteora DLMM

**Parameters:**
- `pool_address`: Address pool Meteora DLMM

**Example:**
```
/lp_pool_info pool_address:LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo
```

### `/lp_add`
Add liquidity ke Meteora DLMM pool dengan fee preview

**Parameters:**
- `pool_address`: Address pool Meteora DLMM
- `token_x_amount`: Amount token X (dalam token units)
- `token_y_amount`: Amount token Y (dalam token units)
- `token_x_mint`: Mint address untuk token X
- `token_y_mint`: Mint address untuk token Y
- `strategy` (optional): Strategy type - `spot`, `curve`, atau `bid_ask` (default: `spot`)
- `min_price` (optional): Minimum price untuk range
- `max_price` (optional): Maximum price untuk range
- `slippage_bps` (optional): Slippage tolerance in basis points (100 = 1%, default: 100)
- `position_address` (optional): Existing position address (kosongkan untuk create new)

**Strategies:**
- **spot**: Distribusi likuiditas merata di seluruh rentang harga (SpotBalanced)
- **curve**: Konsentrasi likuiditas di sekitar harga tengah (Curve)
- **bid_ask**: Konsentrasi likuiditas di ujung-ujung rentang harga (BidAsk)

**Fee Preview:**
Bot akan menampilkan preview dengan breakdown fees sebelum execute:
- ✅ **Refundable Fees**: Dapat dikembalikan saat close position
  - Position Rent: ~0.059 SOL (untuk new position)
  - Extension Rent: ~0.002 SOL per extension (jika range > 69 bins)
- ❌ **Non-Refundable Fees**: Tidak dapat dikembalikan
  - BinArray Creation: ~0.075 SOL per binArray (hanya jika create binArray baru)

Setelah preview, user perlu confirm dengan button sebelum transaction di-execute.

**Example:**
```
/lp_add pool_address:... token_x_amount:1.0 token_y_amount:100.0 token_x_mint:... token_y_mint:... strategy:spot min_price:0.9 max_price:1.1
```

### `/lp_remove`
Remove liquidity dari LP position

**Parameters:**
- `position_address`: Address LP position
- `pool_address`: Address pool Meteora DLMM (required)
- `liquidity_percentage` (optional): Percentage liquidity to remove (0-100, default: 100 = all)
- `from_bin_id` (optional): Start bin ID untuk remove
- `to_bin_id` (optional): End bin ID untuk remove
- `claim_and_close` (optional): Claim fees dan close position setelah remove (default: false)

**Example:**
```
/lp_remove position_address:... pool_address:... liquidity_percentage:50.0 claim_and_close:true
```

### `/lp_positions`
Lihat semua LP positions untuk wallet

**Parameters:**
- `wallet_address` (optional): Wallet address (default: bot wallet)

**Example:**
```
/lp_positions wallet_address:7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU
```

### `/lp_returns`
Calculate estimated LP returns

**Parameters:**
- `pool_address`: Address pool Meteora DLMM
- `token_x_amount`: Amount token X
- `token_y_amount`: Amount token Y
- `days` (optional): Number of days untuk projection (default: 7)

**Example:**
```
/lp_returns pool_address:... token_x_amount:1.0 token_y_amount:100.0 days:7
```

### `/lp_monitor`
Monitor LP position status

**Parameters:**
- `position_address`: Address LP position

**Example:**
```
/lp_monitor position_address:7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU
```

## Architecture

### MeteoraLPAgent Class

Class utama untuk LP operations:

```python
from meteora_lp_agent import get_lp_agent

agent = get_lp_agent()

# Get pool info
pool_info = await agent.get_pool_info(pool_address)

# Get LP positions
positions = await agent.get_lp_positions(wallet_address)

# Calculate returns
returns = await agent.calculate_lp_returns(
    pool_address,
    token_x_amount,
    token_y_amount,
    days=7
)

# Monitor position
status = await agent.monitor_position(position_address)
```

## Solana MCP Integration

Agent menggunakan Solana MCP untuk:
- Query blockchain data
- Get account information
- Transaction building (future)

MCP endpoint dapat dikonfigurasi via `SOLANA_MCP_URL` environment variable.

## Meteora DLMM Program

Program ID: `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo`

## API Endpoints

- **Meteora DLMM API**: `https://dlmm-api.meteora.ag/pair/all_by_groups`
- **Meteora DAMM v2 API**: `https://damm-v2.meteora.ag/pools`
- **Solana MCP**: `https://mcp.solana.com/mcp` (default)

## Future Enhancements

- ✅ Add liquidity transaction building
- ✅ Remove liquidity transaction building
- ✅ Automatic position rebalancing
- ✅ Risk management features
- ✅ Historical performance tracking

## Notes

- **Meteora SDK**: Agent menggunakan Meteora TypeScript SDK via Node.js wrapper untuk build transaction
- Install SDK dengan: `npm install @meteora-ag/dlmm @solana/web3.js bn.js`
- Lihat `METEORA_SDK_SETUP.md` untuk setup instructions
- Agent dapat berjalan dalam mode read-only jika `LP_WALLET_PRIVATE_KEY` tidak diset
- Semua operations menggunakan async/await untuk performa optimal
- Jika SDK tidak terinstall, agent akan fallback ke Meteora API (jika tersedia)

## Troubleshooting

### "LP Agent tidak tersedia"
- Pastikan dependencies terinstall: `pip install -r requirements.txt`
- Check bahwa file `meteora_lp_agent.py` ada di direktori yang sama

### "LP Agent tidak terinisialisasi"
- Set `LP_WALLET_PRIVATE_KEY` environment variable (optional untuk read-only mode)
- Pastikan private key format benar (base58 encoded)

### "Pool tidak ditemukan"
- Verify pool address benar
- Check bahwa pool masih aktif di Meteora

## References

- [Solana MCP Official](https://github.com/solana-foundation/solana-mcp-official)
- [Meteora Documentation](https://docs.meteora.ag/)
- [Meteora DLMM Program](https://app.meteora.ag/)

