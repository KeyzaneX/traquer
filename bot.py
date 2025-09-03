import os
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, List
import traceback
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

# ========= Config =========
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.5"))

API_BASE = "https://bubble-portal.com/api/characters/Thana"

STATE_FILE = Path("xp_state.json")    # { "<id>": {"last_xp": int, "name": str, "level": int} }
WATCH_FILE = Path("xp_targets.json")  # { "<id>": [<user_id>, ...] }

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# Charge l'état + cibles (compat liste -> dict)
STATE: Dict[str, Dict] = load_json(STATE_FILE, {})
_raw_watch = load_json(WATCH_FILE, [])
if isinstance(_raw_watch, list):
    # Migration: ancienne version (liste d'IDs) -> dict id -> []
    WATCH: Dict[str, List[int]] = {cid: [] for cid in _raw_watch if isinstance(cid, str)}
else:
    # Normal: dict id -> [user_ids]
    WATCH = {str(k): [int(u) for u in set(v) if isinstance(u, int) or str(u).isdigit()]
             for k, v in _raw_watch.items()} if isinstance(_raw_watch, dict) else {}

# ========= Discord setup =========
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

session: Optional[aiohttp.ClientSession] = None
channel: Optional[discord.TextChannel] = None

# ========= Helpers =========

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def build_url(char_id: str) -> str:
    return f"{API_BASE}/{char_id}"

async def fetch_char(char_id: str) -> Optional[dict]:
    assert session is not None
    url = build_url(char_id)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                return None
    except asyncio.CancelledError:
        raise
    except Exception:
        return None

def chunk_text(s: str, max_len: int = 1900):
    """Découpe un long texte en morceaux < max_len (sécurité limite Discord ~2000)."""
    out, buf = [], []
    cur = 0
    for line in s.splitlines():
        if cur + len(line) + 1 > max_len:
            out.append("\n".join(buf))
            buf, cur = [line], len(line)
        else:
            buf.append(line)
            cur += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out

def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")

async def notify_xp_change(char_id: str, before: int, after: int, name: str, level: int):
    """Envoie un embed + mentionne les utilisateurs qui suivent cet ID."""
    assert channel is not None
    delta = after - before
    arrow = "⬆️" if delta > 0 else ("⬇️" if delta < 0 else "➡️")
    color = discord.Color.green() if delta > 0 else (discord.Color.red() if delta < 0 else discord.Color.blurple())

    embed = discord.Embed(title=f"Changement d’XP {arrow}", color=color)
    embed.add_field(name="Nom", value=name, inline=True)
    embed.add_field(name="ID", value=char_id, inline=True)
    embed.add_field(name="Niveau", value=str(level), inline=True)
    embed.add_field(name="XP avant", value=fmt_int(before), inline=True)
    embed.add_field(name="XP après", value=fmt_int(after), inline=True)
    embed.add_field(name="Variation", value=f"{delta:+,}".replace(",", " "), inline=True)
    embed.add_field(name="Dernière mise à jour", value=now_str(), inline=False)


    # Mentions des suiveurs de cet ID
    followers = WATCH.get(char_id, [])
    mentions = " ".join(f"<@{uid}>" for uid in set(followers)) if followers else None

    await channel.send(content=mentions, embed=embed)

async def safe_followup(interaction, content=None, embed=None, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except Exception:
        print("[safe_followup] error:\n", traceback.format_exc())

# ========= Commands =========
@tree.command(name="add", description="Commencer à suivre un personnage via son ID numérique")
@app_commands.describe(char_id="L'ID du personnage (numérique)")
async def add_cmd(interaction: discord.Interaction, char_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        if not char_id.isdigit():
            await interaction.followup.send("❌ Merci de fournir un **ID numérique** valide.", ephemeral=True)
            return

        # Valider l'ID + récupérer nom/level/xp pour seed le cache
        data = await fetch_char(char_id)
        if not data or "experience" not in data:
            await interaction.followup.send("❌ ID introuvable ou API indisponible.", ephemeral=True)
            return

        name = data.get("name", "Inconnu")
        level = int(data.get("level", 0))
        xp = int(data.get("experience", 0))

        # Ajoute l'ID à la watchlist + mémorise l'utilisateur
        user_id = interaction.user.id
        followers = WATCH.get(char_id, [])
        was_following = user_id in followers
        if not was_following:
            followers.append(user_id)
        WATCH[char_id] = followers
        save_json(WATCH_FILE, WATCH)

        # Seed/MàJ cache
        STATE[char_id] = {"last_xp": xp, "name": name, "level": level, "last_update": now_str()}
        save_json(STATE_FILE, STATE)

        if was_following:
            await interaction.followup.send(f"ℹ️ Tu suis déjà **{name}** (ID `{char_id}`).", ephemeral=True)
        else:
            await interaction.followup.send(
                f"👀 Tu suivras désormais **{name}** (ID `{char_id}`, niv {level}). "
                f"Je te ping si son XP change.", ephemeral=True
            )

    except Exception:
        print("[/add] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /add.", ephemeral=True)

@tree.command(name="delete", description="Arrêter de suivre un personnage (toi), ou le supprimer s'il n'a aucun suiveur")
@app_commands.describe(char_id="L'ID du personnage (numérique)")
async def delete_cmd(interaction: discord.Interaction, char_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        followers = WATCH.get(char_id, [])

        # ✅ Si aucun suiveur => on supprime totalement l'ID du traqueur
        if followers == []:
            removed_watch = WATCH.pop(char_id, None)  # None si n'existait pas
            removed_state = STATE.pop(char_id, None)
            save_json(WATCH_FILE, WATCH)
            save_json(STATE_FILE, STATE)
            if removed_watch is not None or removed_state is not None:
                await interaction.followup.send(f"🗑️ **{char_id}** supprimé complètement du traqueur.", ephemeral=True)
            else:
                await interaction.followup.send("ℹ️ Cet ID n'était pas présent.", ephemeral=True)
            return

        # Cas normal : enlever SEULEMENT toi des suiveurs
        user_id = interaction.user.id
        if user_id in followers:
            followers = [u for u in followers if u != user_id]
            if followers:
                WATCH[char_id] = followers
            else:
                WATCH.pop(char_id, None)
                STATE.pop(char_id, None)  # facultatif: retirer aussi l'état quand plus de suiveurs
            save_json(WATCH_FILE, WATCH)
            save_json(STATE_FILE, STATE)
            name = STATE.get(char_id, {}).get("name")
            label = f"**{name}** (ID `{char_id}`)" if name else f"ID `{char_id}`"
            await interaction.followup.send(f"✅ Tu ne suis plus {label}.", ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ Tu ne suivais pas cet ID. (S'il n'a **aucun suiveur**, refais `/delete` pour le supprimer complètement.)", ephemeral=True)

    except Exception:
        import traceback
        print("[/delete] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /delete.", ephemeral=True)


@tree.command(name="list", description="Lister les personnages que TU suis")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        user_id = interaction.user.id
        # IDs suivis par CET utilisateur
        my_ids = [cid for cid, users in WATCH.items() if user_id in users]
        if not my_ids:
            await interaction.followup.send("📭 Tu ne suis encore aucun personnage.", ephemeral=True)
            return

        lines = []
        for char_id in sorted(my_ids):
            # Valeurs actuelles du cache
            entry = STATE.get(char_id, {})
            name = entry.get("name")
            last_up = entry.get("last_update")

            # Si le nom n'est pas connu, on résout via l'API et on seed le cache (sans toucher last_update)
            if not name:
                data = await fetch_char(char_id)
                if data:
                    name = data.get("name", "Inconnu")
                    level = int(data.get("level", 0))
                    xp = int(data.get("experience", 0))
                    STATE[char_id] = {
                        "last_xp": xp,
                        "name": name,
                        "level": level,
                        # on préserve last_update s'il existait déjà
                        **({"last_update": last_up} if last_up else {})
                    }
                    # relit proprement depuis STATE (au cas où)
                    entry = STATE.get(char_id, {})
                    last_up = entry.get("last_update")

            # Ligne d'affichage
            if last_up:
                lines.append(f"• **{name or 'Inconnu'}** (`{char_id}`) — dernière actualisation: {last_up}")
            else:
                lines.append(f"• **{name or 'Inconnu'}** (`{char_id}`)")

        save_json(STATE_FILE, STATE)
        await interaction.followup.send("Tu suis :\n" + "\n".join(lines), ephemeral=True)

    except Exception:
        import traceback
        print("[/list] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /list.", ephemeral=True)



@tree.command(name="listall", description="Lister tous les personnages suivis sur le serveur")
async def listall_cmd(interaction: discord.Interaction):
    # Réponse différée en public (ephemeral=False => visible par tous)
    await interaction.response.defer(ephemeral=False)
    try:
        if not WATCH:
            await interaction.followup.send("📭 Aucun personnage n’est suivi pour l’instant.", ephemeral=False)
            return

        lines = []
        for char_id, followers in sorted(WATCH.items(), key=lambda kv: kv[0]):
            entry = STATE.get(char_id, {})  # {"last_xp": ..., "name": ..., "level": ..., "last_update": ...}
            name = entry.get("name")
            last_up = entry.get("last_update")  # ex: "2025-09-02 15:42:18"

            # Si on ne connaît pas encore le nom, on le résout, mais on NE met PAS last_update ici
            if not name:
                data = await fetch_char(char_id)
                if data:
                    name = data.get("name", "Inconnu")
                    level = int(data.get("level", 0))
                    xp = int(data.get("experience", 0))
                    # Seed sans last_update (on ne veut pas confondre "liste" et "maj d'XP")
                    STATE[char_id] = {
                        "last_xp": xp,
                        "name": name,
                        "level": level,
                        **({"last_update": last_up} if last_up else {})  # on préserve si déjà présent
                    }
                    # refresh entry
                    entry = STATE.get(char_id, {})
                    last_up = entry.get("last_update")

            followers = [int(u) for u in followers if str(u).isdigit()]
            mentions = " ".join(f"<@{u}>" for u in followers) if followers else "_personne_"
            last_up_str = f" — dernière actualisation: {last_up}" if last_up else ""
            lines.append(f"• **{name or 'Inconnu'}** (`{char_id}`) — {len(followers)} suiveur(s): {mentions}{last_up_str}")

        save_json(STATE_FILE, STATE)

        text = "📋 **Tous les personnages suivis :**\n" + "\n".join(lines)
        # Si jamais trop long, on coupe en plusieurs messages
        if len(text) > 1900:
            chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=False)
        else:
            await interaction.followup.send(text, ephemeral=False)

    except Exception:
        import traceback
        print("[/listall] error:\n", traceback.format_exc())
        try:
            await interaction.followup.send("⚠️ Erreur interne sur /listall.", ephemeral=False)
        except:
            pass



# ========= Polling loop =========
async def poll_loop():
    await client.wait_until_ready()
    global channel
    channel = await client.fetch_channel(CHANNEL_ID)
    if channel is None:
        print("⚠️ CHANNEL_ID invalide ou inaccessible.")
        return

    while not client.is_closed():
        try:
            for char_id in list(WATCH.keys()):
                data = await fetch_char(char_id)
                if not data:
                    continue

                name = data.get("name", STATE.get(char_id, {}).get("name", "Inconnu"))
                level = int(data.get("level", STATE.get(char_id, {}).get("level", 0)))
                xp = int(data.get("experience", STATE.get(char_id, {}).get("last_xp", 0)))

                if char_id not in STATE:
                    STATE[char_id] = {"last_xp": xp, "name": name, "level": level}
                    continue

                prev = int(STATE[char_id].get("last_xp", 0))
                if xp != prev:
                    STATE[char_id] = {"last_xp": xp, "name": name, "level": level}
                    save_json(STATE_FILE, STATE)
                    await notify_xp_change(char_id, prev, xp, name, level)
                else:
                    # Optionnel: notif de level up
                    prev_lvl = int(STATE[char_id].get("level", 0))
                    if level != prev_lvl:
                        STATE[char_id]["level"] = level
                        STATE[char_id]["last_update"] = now_str()
                        save_json(STATE_FILE, STATE)
                        embed = discord.Embed(
                            title="🎉 Niveau augmenté",
                            description=f"**{name}** (ID `{char_id}`) passe **{prev_lvl} ➜ {level}**",
                            color=discord.Color.gold()
                        )
                        followers = WATCH.get(char_id, [])
                        mentions = " ".join(f"<@{uid}>" for uid in set(followers)) if followers else None
                        await channel.send(content=mentions, embed=embed)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[poll_loop] erreur: {e}")

        await asyncio.sleep(POLL_INTERVAL)

# ========= Events =========
@client.event
async def on_ready():
    global session
    session = aiohttp.ClientSession()
    try:
        GUILD_ID = os.getenv("GUILD_ID")
        if GUILD_ID and GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            # Optionnel : si tu veux repartir propre propre :
            # await tree.sync(guild=guild, commands=[])   # efface d’abord les commandes de guilde
            # Puis sync
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)  # sync immédiat de la guilde

            # (Optionnel) log pour vérifier ce qui est enregistré :
            cmds = await tree.fetch_commands(guild=guild)
            print("✅ Guild commands:", [c.name for c in cmds])
        else:
            # Fallback global (peut prendre du temps à apparaître)
            await tree.sync()
            print("✅ Slash commands synchronisées globalement (peut prendre jusqu'à 1h).")
    except Exception as e:
        print(f"Erreur sync commands: {e}")

    client.loop.create_task(poll_loop())


@client.event
async def on_disconnect():
    save_json(STATE_FILE, STATE)
    save_json(WATCH_FILE, WATCH)

# ========= Main =========
async def main():
    async with client:
        await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if not DISCORD_TOKEN or CHANNEL_ID == 0:
        raise SystemExit("⚠️ Configure DISCORD_TOKEN et DISCORD_CHANNEL_ID dans .env")
    try:
        asyncio.run(main())
    finally:
        if session and not session.closed:
            asyncio.run(session.close())
