# Discord Bot - Meteora DLMM Pool Checker

## Overview
A Discord bot that checks Meteora DLMM pools for any given token address. The bot fetches pool data from the Meteora API and displays the top 10 pools sorted by liquidity.

## Current State
- **Status**: Deployed and running 24/7 on Replit
- **Bot Username**: token-check#8157
- **Commands**: 1 slash command synced and ready

## Features
- `/check <token_address>` - Displays top 10 Meteora DLMM pools for the specified token
  - Shows pool pair names (formatted as TOKEN1-TOKEN2)
  - Displays bin step configuration
  - Shows liquidity in USD (formatted as $X.XK for thousands)
  - Provides clickable links to view pools on Meteora app
  - Sorts pools by liquidity (highest first)

## Project Structure
```
.
├── main.py              # Main bot code with /check command
├── pyproject.toml       # Python dependencies (discord.py, requests)
├── uv.lock              # Lock file for dependencies
└── .gitignore           # Excludes sensitive files and Python artifacts
```

## Dependencies
- **discord.py** (2.6.4) - Discord bot framework
- **requests** (2.32.5) - HTTP library for API calls
- **Python** 3.11

## Configuration
### Environment Variables (Secrets)
- `DISCORD_BOT_TOKEN` - Discord bot authentication token (required)
- `SESSION_SECRET` - Session management secret (available but not used)

### Workflow
- **Name**: Discord Bot
- **Command**: `python main.py`
- **Output Type**: Console
- **Auto-restart**: Enabled

## API Integration
The bot integrates with the Meteora DLMM API:
- **Endpoint**: `https://dlmm-api.meteora.ag/pair/all?include_unknown=true`
- **Purpose**: Fetches all available pool pairs with their liquidity data
- **Response**: JSON array of pool objects with mint addresses, names, liquidity, bin steps, etc.

## How It Works
1. User invokes `/check <token_address>` in Discord
2. Bot fetches all pools from Meteora API
3. Filters pools where token_address matches either mint_x or mint_y
4. Sorts matching pools by liquidity (descending)
5. Formats and displays top 10 pools with clickable links
6. Shows total count if more than 10 pools exist

## Security
- Bot token stored securely in Replit Secrets (not in code)
- .gitignore prevents accidental commit of sensitive files
- No hardcoded credentials in source code

## Recent Changes
- **2025-11-10**: Initial deployment to Replit
  - Migrated from standalone script to Replit environment
  - Added secure token management via environment variables
  - Configured continuous operation workflow
  - Added proper .gitignore for Python projects

## Usage Instructions
To use the bot in Discord:
1. Invite the bot to your Discord server
2. Use the command: `/check <token_address>`
3. Replace `<token_address>` with the Solana token mint address you want to check

Example: `/check So11111111111111111111111111111111111111112`

## Maintenance Notes
- Bot runs continuously via Replit workflow
- Automatically reconnects if disconnected
- Logs show connection status and command sync information
- No manual intervention required for normal operation
