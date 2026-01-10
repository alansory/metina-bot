# Meteora SDK Setup Guide

Meteora menyediakan SDK resmi untuk TypeScript/JavaScript. Untuk menggunakan SDK ini dari Python, kita menggunakan wrapper yang memanggil Node.js.

## Install Meteora SDK

### 1. Install Node.js dan npm

Pastikan Node.js dan npm sudah terinstall:

```bash
node --version
npm --version
```

### 2. Install Meteora DLMM SDK

Install Meteora SDK dan dependencies:

```bash
npm install @meteora-ag/dlmm @solana/web3.js bn.js
```

Atau jika menggunakan project directory:

```bash
cd /path/to/project
npm init -y
npm install @meteora-ag/dlmm @solana/web3.js bn.js
```

### 3. Verify Installation

Test apakah SDK terinstall dengan benar:

```bash
node -e "require('@meteora-ag/dlmm'); console.log('SDK installed!')"
```

## Menggunakan SDK Wrapper

Setelah SDK terinstall, Python agent akan otomatis menggunakan SDK wrapper untuk build transaction.

### Environment Variables

```bash
# Optional: Custom Node.js path
NODE_PATH=/usr/local/bin/node

# Optional: Custom npm path
NPM_PATH=/usr/local/bin/npm

# RPC URL (default: https://api.mainnet-beta.solana.com)
RPC_URL=https://api.mainnet-beta.solana.com
# atau
HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

## Cara Kerja

1. Python agent memanggil `meteora_sdk_wrapper.py`
2. Wrapper membuat temporary Node.js script
3. Script menggunakan Meteora SDK untuk build transaction
4. Transaction di-serialize ke base64
5. Python agent sign dan send transaction

## Troubleshooting

### "Meteora SDK not installed"

Install SDK dengan:
```bash
npm install @meteora-ag/dlmm @solana/web3.js bn.js
```

### "Node.js not found"

Pastikan Node.js ada di PATH, atau set `NODE_PATH` environment variable.

### "Script execution timeout"

- Check RPC connection
- Increase timeout di `meteora_sdk_wrapper.py`
- Verify pool address valid

## Alternative: Manual Transaction Building

Jika tidak ingin menggunakan SDK, agent akan fallback ke:
1. Meteora API endpoint (jika tersedia)
2. Error message dengan detail parameter

Untuk implementasi manual, diperlukan:
- Anchor program IDL untuk Meteora DLMM
- Instruction encoding manual
- Account derivation

## References

- [Meteora DLMM SDK Docs](https://docs.meteora.ag/developer-guide/guides/dlmm/typescript-sdk/getting-started)
- [Meteora SDK GitHub](https://github.com/MeteoraAg/dlmm-sdk)

