# Cara Test Bot Call untuk Token Baru

## 📋 Overview

Script test ini digunakan untuk melihat token apa yang akan dikirim oleh bot call ke Discord channel sebelum bot benar-benar mengirimnya.

## 🚀 Cara Menjalankan Test

### 1. Install Dependencies

Pastikan semua dependencies sudah terinstall:

```bash
cd metina-bot
pip install -r requirements.txt
```

### 2. Pilih API Source

Test script mendukung 2 API source:

#### Option A: Jupiter API (Recommended - lebih akurat)
```bash
# Default menggunakan Jupiter API
python3 test_bot_call_fetch.py

# Atau set environment variable
export USE_JUPITER_API=true
export JUPITER_API_KEY=your_api_key_here
python3 test_bot_call_fetch.py
```

#### Option B: DexScreener API (Tidak perlu API key)
```bash
export USE_JUPITER_API=false
python3 test_bot_call_fetch.py
```

### 3. Jalankan Test Script

```bash
python3 test_bot_call_fetch.py
```

## 📊 Apa yang Ditest?

Script ini akan:

1. **Fetch tokens** dari Jupiter API (Top Traded 1h) atau DexScreener API
2. **Filter tokens** berdasarkan kriteria:
   - ✅ Chain: Solana ONLY
   - ✅ Market Cap: $250,000 - $10,000,000 USD
   - ✅ Total Fees: >= 20 SOL (dari volume 24h)
   - ✅ Token baru (umur <= 2 jam) atau fees tinggi (>= 40 SOL)
3. **Calculate score** untuk setiap token (market cap 60% + fees 40%)
4. **Tampilkan hasil** token terbaik yang akan dikirim ke Discord

## 🔍 Kriteria Filter

- **Market Cap Range**: $250,000 - $10,000,000 USD
- **Fees Minimum**: 20 SOL (dari volume 24h dengan fee 0.3%)
- **Token Baru**: Umur <= 2 jam
- **Token Lama**: Jika > 2 jam, harus fees >= 40 SOL (2x minimum)
- **API Endpoint**: Jupiter Top Traded 1h (limit 100 tokens)

## 📝 Output

Script akan menampilkan:

1. **Proses Fetching**: Token yang ditemukan dari API
2. **Filtering Process**: Token yang dicek dan apakah memenuhi kriteria
3. **Qualifying Tokens**: Daftar token yang memenuhi kriteria (sorted by score)
4. **Best Token**: Token terbaik yang akan dikirim ke Discord

## ⚙️ Konfigurasi

### Kriteria Filter

Anda bisa mengubah kriteria di bagian atas file `test_bot_call_fetch.py`:

```python
BOT_CALL_MIN_MARKET_CAP = 250000  # 250k USD
BOT_CALL_MAX_MARKET_CAP = 10000000  # 10jt USD
BOT_CALL_MIN_FEES_SOL = 20  # 20 SOL
```

**Note**: Endpoint yang digunakan adalah `https://api.jup.ag/tokens/v2/toptraded/1h?limit=100` untuk mendapatkan top traded tokens dalam 1 jam terakhir.

### API Source

Pilih API source dengan environment variable:

```bash
# Gunakan Jupiter API (default, lebih akurat)
export USE_JUPITER_API=true
export JUPITER_API_KEY=your_api_key_here

# Atau gunakan DexScreener (tidak perlu API key)
export USE_JUPITER_API=false
```

## 🔄 Perbedaan dengan Main Bot

- **Test Script**: Bisa menggunakan Jupiter API (default) atau DexScreener API
- **Main Bot**: Menggunakan Jupiter API (perlu API key)

Kedua menggunakan logika filtering yang sama. Jupiter API lebih akurat untuk menemukan token baru.

## ❓ Troubleshooting

### Tidak ada token yang ditemukan?

Kemungkinan:
- Tidak ada token baru yang memenuhi kriteria saat ini
- Market cap atau fees terlalu rendah
- API rate limited (tunggu beberapa saat)

### Error saat fetch?

- Pastikan koneksi internet stabil
- Cek apakah DexScreener API sedang down
- Pastikan `aiohttp` sudah terinstall

## 📌 Catatan

- Test ini **TIDAK** mengirim notifikasi ke Discord
- Test ini hanya untuk melihat token apa yang akan dikirim
- Untuk test dengan Discord, gunakan bot dengan `BOT_CALL_CHANNEL_ID` yang di-set

