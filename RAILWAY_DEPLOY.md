# Railway Deployment Guide

## Auto-Install Meteora SDK

Railway akan otomatis install Meteora SDK saat deploy jika konfigurasi berikut ada:

### Files yang diperlukan:

1. **package.json** - Dependencies Node.js (Meteora SDK)
2. **railway.json** - Build configuration (optional)
3. **nixpacks.toml** - Nixpacks configuration untuk multi-language support

### Setup di Railway:

1. **Detect Buildpacks:**
   - Railway akan auto-detect `package.json` dan install npm packages
   - Railway akan auto-detect `requirements.txt` dan install Python packages

2. **Build Command:**
   - Jika menggunakan `nixpacks.toml`, **JANGAN** set `buildCommand` di `railway.json`
   - Semua build steps sudah didefinisikan di `nixpacks.toml` phases
   - Jika tidak menggunakan `nixpacks.toml`, bisa set di `railway.json`:
   ```json
   {
     "build": {
       "buildCommand": ". /app/venv/bin/activate && pip install -r requirements.txt && npm install"
     }
   }
   ```

3. **Start Command:**
   - Jika menggunakan virtual environment (recommended):
   ```
   . /app/venv/bin/activate && python main.py
   ```
   - Atau jika tidak menggunakan venv:
   ```
   python3 main.py
   ```

### Verifikasi:

Setelah deploy, check logs untuk:
```
📦 Installing Node.js dependencies (Meteora SDK)...
✅ Meteora SDK installed successfully
```

### Troubleshooting:

#### SDK tidak terinstall:
1. Check Railway logs untuk error npm install
2. Pastikan Node.js tersedia di build environment
3. Check `package.json` syntax valid

#### Build fails dengan error "undefined variable 'npm'":
1. **PENTING**: Jangan include `npm` sebagai package terpisah di `nixpacks.toml`
2. `npm` sudah termasuk dengan `nodejs-18_x`, tidak perlu ditambahkan
3. Pastikan `nixpacks.toml` hanya berisi: `nixPkgs = ["python3Full", "nodejs-18_x"]`
4. Jangan gunakan: `nixPkgs = ["python39", "nodejs-18_x", "npm"]` ❌

#### Build fails dengan error "No module named pip" di phase build:
1. **PENTING**: Jika menggunakan `nixpacks.toml`, **HAPUS** `buildCommand` dari `railway.json`
2. Semua install steps harus di `nixpacks.toml` phase `install`, bukan di `railway.json`
3. Pastikan `railway.json` hanya berisi:
   ```json
   {
     "build": {
       "builder": "NIXPACKS"
     },
     "deploy": {
       "startCommand": ". /app/venv/bin/activate && python main.py"
     }
   }
   ```
4. Jangan set `buildCommand` di `railway.json` jika sudah ada `nixpacks.toml` ❌

#### Build fails dengan error "pip: command not found" atau "No module named pip":
1. **PENTING**: Python di Nix tidak include pip secara default
2. **Solusi yang bekerja**: Gunakan `python3Full` yang sudah include pip dan venv
3. Gunakan virtual environment untuk avoid "externally-managed-environment" error:
   ```toml
   [phases.setup]
   nixPkgs = ["python3Full", "nodejs-18_x"]
   
   [phases.install]
   cmds = [
       "python3 -m venv /app/venv",
       ". /app/venv/bin/activate && pip install --upgrade pip",
       ". /app/venv/bin/activate && pip install -r requirements.txt",
       "npm install"
   ]
   
   [start]
   cmd = ". /app/venv/bin/activate && python main.py"
   ```

#### Build fails dengan error "externally-managed-environment":
1. **PENTING**: Python di Nix adalah immutable dan tidak bisa diinstall package langsung
2. **Solusi**: Gunakan virtual environment (venv) untuk install packages
3. Atau gunakan flag `--break-system-packages` (hanya untuk containerized environments)
4. **Recommended**: Gunakan `python3Full` dengan virtual environment seperti di atas ✅

#### Build fails lainnya:
1. Pastikan `package.json` ada di root directory
2. Check Node.js version compatibility (>=18.0.0)
3. Verify npm dependencies versions

### Manual Setup (jika auto-install gagal):

Jika Railway tidak auto-install, pastikan konfigurasi di `nixpacks.toml` sudah benar:
```toml
[phases.setup]
nixPkgs = ["python3Full", "nodejs-18_x"]

[phases.install]
cmds = [
    "python3 -m venv /app/venv",
    ". /app/venv/bin/activate && pip install --upgrade pip",
    ". /app/venv/bin/activate && pip install -r requirements.txt",
    "npm install"
]

[start]
cmd = ". /app/venv/bin/activate && python main.py"
```

### Environment Variables:

Pastikan set di Railway:
- `DISCORD_BOT_TOKEN`
- `LP_WALLET_PRIVATE_KEY` (optional)
- `HELIUS_RPC_URL` atau `RPC_URL`
- `SOLANA_MCP_URL` (optional)

### Notes:

- Railway menggunakan Nixpacks untuk build
- Nixpacks akan detect Python dan Node.js dari files yang ada
- `package.json` akan trigger npm install
- `requirements.txt` akan trigger pip install
- Both akan run automatically during build phase

