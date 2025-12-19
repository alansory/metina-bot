# Cara Test Bot Call - Kirim ke Discord

## 📋 Overview

Script `test_bot_call_send.py` digunakan untuk test mengirim bot call notification langsung ke Discord channel tanpa menunggu scheduled task.

## 🚀 Cara Menggunakan

### 1. Set Environment Variables

```bash
# Discord Bot Token (wajib)
export DISCORD_BOT_TOKEN=your_discord_bot_token

# Channel ID untuk bot call (wajib)
export BOT_CALL_CHANNEL_ID=your_channel_id

# Optional: API source
export USE_JUPITER_API=true  # atau false untuk DexScreener
export USE_METEORA_FOR_FEES=false  # true untuk fetch fees dari Meteora
```

### 2. Jalankan Test Script

```bash
cd metina-bot-discord/metina-bot
python3 test_bot_call_send.py
```

## 📝 Apa yang Dilakukan Script?

1. **Fetch tokens** dari Jupiter API (atau DexScreener)
2. **Filter tokens** berdasarkan kriteria:
   - Market Cap: $250k - $10jt
   - Fees: >= 20 SOL
3. **Pilih best token** (highest score)
4. **Kirim ke Discord** channel yang ditentukan

## 🔍 Cara Dapat Channel ID

1. Buka Discord
2. Enable Developer Mode (Settings → Advanced → Developer Mode)
3. Right-click pada channel yang ingin digunakan
4. Pilih "Copy ID"
5. Gunakan ID tersebut sebagai `BOT_CALL_CHANNEL_ID`

## ⚠️ Requirements

- `discord.py` harus terinstall: `pip install discord.py`
- Bot harus sudah di-invite ke server dengan permission:
  - Send Messages
  - Embed Links
  - View Channels

## 🔧 Troubleshooting

### Error: "DISCORD_BOT_TOKEN not set"
- Pastikan environment variable sudah di-set
- Cek dengan: `echo $DISCORD_BOT_TOKEN`

### Error: "Channel not found"
- Pastikan Channel ID benar
- Pastikan bot punya akses ke channel tersebut
- Pastikan bot sudah di-invite ke server

### Error: "discord.py not installed"
```bash
pip install discord.py
```

### Bot tidak bisa kirim message
- Pastikan bot punya permission "Send Messages"
- Pastikan bot punya permission "Embed Links"
- Cek role permissions di channel

## 📌 Catatan

- Script ini **TIDAK** menyimpan state (tidak track token yang sudah dikirim)
- Setiap kali dijalankan akan kirim token terbaik saat ini
- Untuk production, gunakan `main.py` yang sudah ada scheduled task dan state tracking

