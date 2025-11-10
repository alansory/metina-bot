import discord
from discord.ext import commands
import requests
import os

TOKEN = os.getenv('DISCORD_BOT_TOKEN')

if not TOKEN:
    print("ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    print("Please add your Discord bot token to the Secrets tab.")
    exit(1)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} sudah online!')
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} command(s)')
    except Exception as e:
        print(e)

@bot.tree.command(name='check', description='Cek 10 pool DLMM Meteora teratas untuk token')
async def check_pools(interaction: discord.Interaction, token_address: str):
    await interaction.response.defer()
    
    try:
        url = 'https://dlmm-api.meteora.ag/pair/all?include_unknown=true'
        response = requests.get(url)
        response.raise_for_status()
        pools = response.json()
        
        matching_pools = []
        for pool in pools:
            mint_x = pool.get('mint_x', '').lower()
            mint_y = pool.get('mint_y', '').lower()
            if token_address.lower() in [mint_x, mint_y]:
                name = pool.get('name', '').strip()
                if name:
                    clean_name = name.replace(' DLMM', '').replace('DLMM', '').strip()
                    if '/' in clean_name or '-' in clean_name:
                        separator = '/' if '/' in clean_name else '-'
                        parts = clean_name.split(separator)
                        if len(parts) >= 2:
                            name_x = parts[0].strip()
                            name_y = parts[1].strip()
                            pair_name = f"{name_x}-{name_y}"
                        else:
                            pair_name = clean_name
                    else:
                        pair_name = clean_name
                else:
                    matching_mint = mint_x if token_address.lower() == mint_x else mint_y
                    pair_name = f"{matching_mint[:8]} Pair"
                
                liq = float(pool.get('liquidity', 0))
                liq_str = f"${liq/1000:.1f}K" if liq >= 1000 else f"${liq:.1f}"
                
                bin_step = pool.get('bin_step', 0)
                bin_format = f"{bin_step}/5"
                
                address = pool.get('address', '')
                
                matching_pools.append({
                    'pair': pair_name,
                    'bin': bin_format,
                    'liq': liq_str,
                    'raw_liq': liq,
                    'address': address
                })
        
        if not matching_pools:
            embed = discord.Embed(title="Pool DLMM Meteora", description=f"Gak ditemuin pool untuk token {token_address[:8]}...", color=0xff0000)
            await interaction.followup.send(embed=embed)
            return
        
        matching_pools.sort(key=lambda x: x['raw_liq'], reverse=True)
        
        top_pools = matching_pools[:10]
        
        description = f"Found {len(matching_pools)} Meteora DLMM pool untuk token: {token_address}\n\n"
        for i, p in enumerate(top_pools, 1):
            if p['address']:
                link = f"https://app.meteora.ag/dlmm/{p['address']}"
                description += f"{i}. [{p['pair']}]({link}) {p['bin']} - LQ: {p['liq']}\n"
            else:
                description += f"{i}. {p['pair']} {p['bin']} - LQ: {p['liq']}\n"
        
        if len(matching_pools) > 10:
            description += f"\n... dan {len(matching_pools) - 10} pool lainnya (LQ lebih rendah)."
        
        embed = discord.Embed(title="Meteora Pool Bot", description=description, color=0x00ff00)
        embed.set_footer(text=f"Requested by {interaction.user} | Top 10 by LQ")
        await interaction.followup.send(embed=embed)
        
    except requests.exceptions.RequestException as e:
        await interaction.followup.send(f"Error fetch API: {str(e)}")
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}")

bot.run(TOKEN)
