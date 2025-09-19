import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime, timedelta
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

notify_channel: Optional[discord.TextChannel] = None

NOTIFY_GUILD_ID = 1417905797979181220 #serveur last danse
NOTIFY_CHANNEL_ID = 1418182282971320411 #black-bird channel

ALLOWED_COMMANDS_CHANNEL_ID = 1418182282971320411 #blackbird 
ALLOWED_TRACK_CHANNEL_ID = 1418185821202419842  # james-bond
TRACK_DURATION_SECONDS = 10 * 60                # 10 minutes
TRACK_INTERVAL_SECONDS = 2                      # toutes les 2 secondes


# ========= Config =========
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.5"))

API_BASE = "https://bubble-portal.com/api/characters/Thana"

STATE_FILE = Path("xp_state.json")    # { "<id>": {"last_xp": int, "name": str, "level": int} }
WATCH_FILE = Path("xp_targets.json")  # { "<id>": [<user_id>, ...] }

def ensure_allowed_channel(interaction: discord.Interaction) -> bool:
    """Vérifie si une commande est exécutée dans le bon salon."""
    if not interaction.channel or interaction.channel.id != ALLOWED_COMMANDS_CHANNEL_ID:
        return False
    return True

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
# HELPER POUR CALCULE LA DUREE DES COMBAT
def fmt_duration(delta: timedelta) -> str:
    """Formatte une durée en 'Hh Mm Ss' (sans zéros inutiles)."""
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

def build_char_url_3digits(base_id: str, value: int) -> str:
    """
    Construit l'URL API en remplaçant les 3 derniers chiffres de l'ID par `value` (001..999).
    """
    if len(base_id) < 3 or not base_id[-3:].isdigit():
        return f"https://bubble-portal.com/api/characters/Thana/{base_id}"
    new_id = base_id[:-3] + f"{value:03d}"
    return f"https://bubble-portal.com/api/characters/Thana/{new_id}"


# remplace ton fetch_char_xp par ceci
async def fetch_char_info(char_url: str) -> tuple[int | None, str | None]:
    """
    Retourne (xp, name) via l'API JSON, ou (None, None) si erreur.
    """
    assert session is not None
    try:
        async with session.get(char_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                xp = data.get("experience")
                name = data.get("name")
                return (int(xp) if xp is not None else None, str(name) if name is not None else None)
            return (None, None)
    except asyncio.CancelledError:
        raise
    except Exception:
        return (None, None)


ACTIVE_TRACK_TASKS: dict[int, asyncio.Task] = {}

async def run_precise_xp_tracker(channel_obj: discord.TextChannel, base_id: str):
    """
    Suivi de l'XP pendant 10 minutes :
      - toutes les 2 secondes
      - on modifie les 2 derniers chiffres de l'ID de 001 à 999 en boucle
      - on lit l'XP et on notifie :
          • au lancement (XP initial)
          • uniquement quand l'XP augmente
          • en affichant une durée estimée depuis la dernière augmentation
          • un message final à la fin
    """
    end_time = datetime.now() + timedelta(seconds=TRACK_DURATION_SECONDS)
    last_xp: int | None = None
    last_time: datetime | None = None
    current_name: str | None = None
    counter = 1  # commence à 001

    await channel_obj.send(
        f"🔎 Suivi précis lancé pour **10 minutes**.\n"
        f"ID de base : `{base_id}` (Thana)\n"
        f"tracking toutes les {TRACK_INTERVAL_SECONDS}s."
    )

    try:
        while datetime.now() < end_time:
            char_url = build_char_url_3digits(base_id, counter)
            print(f"[trackxp] Checking URL: {char_url}")  # log console

            xp, name = await fetch_char_info(char_url)
            print(f"[trackxp] XP returned: {xp} | name: {name}")  # log console

            if name and not current_name:
                current_name = name  # on fige le nom dès qu'on l'obtient

            if xp is not None:
                now = datetime.now()

                if last_xp is None:
                    # Première valeur -> annonce initiale
                    last_xp = xp
                    last_time = now
                    who = f"**{current_name}**" if current_name else "le personnage"
                    await channel_obj.send(f"📌 XP initial pour {who} : **{last_xp:,}**".replace(",", " "))

                elif xp > last_xp:
                    # Durée estimée depuis la dernière augmentation
                    elapsed = now - (last_time or now)
                    last_time = now

                    delta_xp = xp - last_xp
                    last_xp = xp

                    who = f"**{current_name}**" if current_name else "Le personnage"
                    duration_str = fmt_duration(elapsed)
                    msg = (
                        f"🎯 {who} a fini son combat et a gagné **{delta_xp:+,}** points d’expérience "
                        f"(total **{last_xp:,}**). ⏱️ Durée estimée : **{duration_str}**"
                    ).replace(",", " ")
                    await channel_obj.send(msg)

                elif xp < last_xp:
                    # Valeur plus petite -> ignorée
                    print(f"[trackxp] XP decreased ({xp} < {last_xp}) — ignored")

            # Avancer le compteur
            counter = counter + 1 if counter < 999 else 1
            await asyncio.sleep(TRACK_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        await channel_obj.send("⏹️ Suivi précis interrompu.")
        raise
    except Exception as e:
        await channel_obj.send(f"❌ Erreur du suivi précis: `{e}`")
        print(f"[trackxp] ERROR: {e}")
    finally:
        await channel_obj.send("✅ Fin du suivi précis (10 minutes écoulées).")
        ACTIVE_TRACK_TASKS.pop(channel_obj.id, None)



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
    """Envoie un embed + mentionne les utilisateurs qui suivent cet ID dans le SALON DÉDIÉ."""
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

    # Mentions + note éventuelle
    followers = WATCH.get(char_id, [])
    mentions = " ".join(f"<@{uid}>" for uid in set(followers)) if followers else None
    desc = STATE.get(char_id, {}).get("description")
    if desc:
        embed.add_field(name="Note", value=desc, inline=False)

    # ➜ ENVOI DANS LE SALON DÉDIÉ
    global notify_channel
    if notify_channel is None:
        # tentative de résolution à la volée (au cas où on_ready n’a pas réussi)
        try:
            guild = client.get_guild(NOTIFY_GUILD_ID)
            if guild:
                notify_channel = guild.get_channel(NOTIFY_CHANNEL_ID)
            if notify_channel is None:
                notify_channel = await client.fetch_channel(NOTIFY_CHANNEL_ID)  # type: ignore
        except Exception as e:
            print(f"[notify_xp_change] ⚠️ Impossible de résoudre notify_channel: {e}")
            return

    if notify_channel is None:
        print(f"[notify_xp_change] ⚠️ notify_channel toujours introuvable (ID {NOTIFY_CHANNEL_ID})")
        return

    try:
        await notify_channel.send(content=mentions, embed=embed)
    except Exception:
        import traceback
        print("[notify_xp_change] error:\n", traceback.format_exc())


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
@app_commands.describe(
    char_id="L'ID du personnage (numérique)",
    description="(Optionnel) Une description / note pour ce personnage",
    notify="Être notifié par un ping en cas de variation d'XP (défaut: oui)"
)
async def add_cmd(
    interaction: discord.Interaction,
    char_id: str,
    description: str | None = None,
    notify: bool = True
):
    # ✅ Vérif du salon autorisé
    if not ensure_allowed_channel(interaction):
        await interaction.response.send_message(
            f"⛔ Cette commande n’est autorisée que dans <#{ALLOWED_COMMANDS_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        if not char_id.isdigit():
            await interaction.followup.send("❌ Merci de fournir un **ID numérique** valide.", ephemeral=True)
            return

        data = await fetch_char(char_id)
        if not data or "experience" not in data:
            await interaction.followup.send("❌ ID introuvable ou API indisponible.", ephemeral=True)
            return

        name = data.get("name", "Inconnu")
        level = int(data.get("level", 0))
        xp = int(data.get("experience", 0))

        user_id = interaction.user.id
        followers = WATCH.get(char_id, [])

        # 🔔 Gestion du paramètre notify
        if notify and user_id not in followers:
            followers.append(user_id)
            WATCH[char_id] = followers
        elif not notify and not followers:
            # si pas de suiveurs mais notify=False, on crée quand même une entrée vide
            WATCH[char_id] = followers

        save_json(WATCH_FILE, WATCH)

        # Seed / MAJ STATE
        entry = STATE.get(char_id, {})
        entry.update({
            "last_xp": xp,
            "name": name,
            "level": level,
            "last_update": now_str()
        })
        if description:
            entry["description"] = description.strip()
        STATE[char_id] = entry
        save_json(STATE_FILE, STATE)

        # Message de confirmation
        if notify:
            extra = f" — _{entry['description']}_" if entry.get("description") else ""
            await interaction.followup.send(
                f"👀 Tu suivras désormais **{name}** (ID `{char_id}`, niv {level}){extra}. "
                f"Je te ping si son XP change.", ephemeral=True
            )
        else:
            extra = f" — _{entry['description']}_" if entry.get("description") else ""
            await interaction.followup.send(
                f"👀 **{name}** (ID `{char_id}`, niv {level}) ajouté au suivi{extra}. "
                f"⚠️ Tu ne seras **pas ping** en cas de variation d'XP.", ephemeral=True
            )

    except Exception:
        import traceback
        print("[/add] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /add.", ephemeral=True)



@tree.command(name="delete", description="Arrêter de suivre un personnage (toi), ou le supprimer s'il n'a aucun suiveur")
@app_commands.describe(char_id="L'ID du personnage (numérique)")
async def delete_cmd(interaction: discord.Interaction, char_id: str):
    if not ensure_allowed_channel(interaction):
        await interaction.response.send_message(
            f"⛔ Cette commande n’est pas autorisée dans ce channel.",
            ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        followers = WATCH.get(char_id, [])

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
        my_ids = [cid for cid, users in WATCH.items() if user_id in users]
        if not my_ids:
            await interaction.followup.send("📭 Tu ne suis encore aucun personnage.", ephemeral=True)
            return

        lines = []
        for char_id in sorted(my_ids):
            entry = STATE.get(char_id, {})
            name = entry.get("name", "Inconnu")
            level = entry.get("level", "?")
            last_up = entry.get("last_update", "Jamais")
            desc = entry.get("description")

            block = f"• **{name}** (ID: `{char_id}`)\n"
            block += f"  Niveau : {level}\n"
            block += f"  Dernière actualisation : {last_up}\n"
            if desc:
                block += f"  Note : {desc}\n"
            lines.append(block)

        text = "📋 **Tes personnages suivis :**\n" + "\n".join(lines)
        await interaction.followup.send(text, ephemeral=True)

    except Exception:
        import traceback
        print("[/list] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /list.", ephemeral=True)





@tree.command(name="listall", description="Lister tous les personnages suivis sur le serveur")
async def listall_cmd(interaction: discord.Interaction):
    # Réponse publique dans le salon
    await interaction.response.defer(ephemeral=False)
    try:
        if not WATCH:
            await interaction.followup.send("📭 Aucun personnage n’est suivi pour l’instant.", ephemeral=False)
            return

        lines = []
        for char_id, followers in sorted(WATCH.items(), key=lambda kv: kv[0]):
            entry = STATE.get(char_id, {})
            name = entry.get("name", "Inconnu")
            level = entry.get("level", "?")
            last_up = entry.get("last_update", "Jamais")
            desc = entry.get("description")

            # Nettoyage des suiveurs -> mentions
            follower_ids = [int(u) for u in followers if str(u).isdigit()]
            mentions = " ".join(f"<@{u}>" for u in follower_ids) if follower_ids else "_personne_"

            block = f"• **{name}** (ID: `{char_id}`)\n"
            block += f"  Niveau : {level}\n"
            block += f"  Suiveurs : {mentions}\n"
            block += f"  Dernière actualisation : {last_up}\n"
            if desc:
                block += f"  Note : {desc}\n"
            lines.append(block)

        text = "🌍 **Tous les personnages suivis :**\n" + "\n".join(lines)

        # Split si trop long pour Discord
        if len(text) > 1900:
            chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=False)
        else:
            await interaction.followup.send(text, ephemeral=False)

    except Exception:
        import traceback
        print("[/listall] error:\n", traceback.format_exc())
        await safe_followup(interaction, "⚠️ Erreur interne sur /listall.", ephemeral=False)



@tree.command(name="trackxp", description="Suivi ultra précis de l’XP pendant 10 minutes (ID du personnage)")
@app_commands.describe(char_id="ID du personnage (numérique)")
async def trackxp_cmd(interaction: discord.Interaction, char_id: str):
    # Autorisé uniquement dans le salon dédié
    if not interaction.channel or interaction.channel.id != ALLOWED_TRACK_CHANNEL_ID:
        await interaction.response.send_message(
            f"⛔ Cette commande n’est autorisée que dans <#{ALLOWED_TRACK_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

    if not char_id.isdigit():
        await interaction.followup.send("❌ Merci de fournir un **ID numérique**.")
        return

    ch_id = interaction.channel.id
    if ch_id in ACTIVE_TRACK_TASKS and not ACTIVE_TRACK_TASKS[ch_id].done():
        await interaction.followup.send("⚠️ Un suivi précis est déjà en cours dans ce salon.")
        return

    ch: discord.TextChannel = interaction.channel  # type: ignore
    task = asyncio.create_task(run_precise_xp_tracker(ch, char_id))
    ACTIVE_TRACK_TASKS[ch_id] = task
    await interaction.followup.send(f"⏱️ Suivi lancé pour l’ID `{char_id}`.")

@tree.command(name="stoptrack", description="Arrêter le suivi précis d'un personnage (ID)")
@app_commands.describe(char_id="ID du personnage (numérique)")
async def stoptrack_cmd(interaction: discord.Interaction, char_id: str):
    # Autorisé uniquement dans le salon dédié
    if not interaction.channel or interaction.channel.id != ALLOWED_TRACK_CHANNEL_ID:
        await interaction.response.send_message(
            f"⛔ Cette commande n’est autorisée que dans <#{ALLOWED_TRACK_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

    # Vérifie si une tâche de suivi est active dans ce salon
    task = ACTIVE_TRACK_TASKS.get(interaction.channel.id)
    if not task or task.done():
        await interaction.followup.send("ℹ️ Aucun suivi précis n'est actif dans ce salon.", ephemeral=True)
        return

    # Annule la tâche (ça déclenchera l'exception asyncio.CancelledError dans run_precise_xp_tracker)
    task.cancel()
    await interaction.followup.send(f"🛑 Suivi précis pour l’ID `{char_id}` arrêté manuellement.")

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
    global session, notify_channel
    session = aiohttp.ClientSession()

    try:
        GUILD_ID = 1417905797979181220 #last danse
        guild = discord.Object(id=GUILD_ID)

        # 🔄 Reset + sync des commandes uniquement pour la guilde (instantané)
        tree.clear_commands(guild=guild)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)

        print(f"✅ Connecté en tant que {client.user} (slash commands synchronisées sur la guilde {GUILD_ID}).")

    except Exception as e:
        print(f"[on_ready] Erreur sync commands: {e}")

    # ➜ Résoudre le salon de notification dédié
    try:
        guild_obj = client.get_guild(1417905797979181220)
        if guild_obj:
            notify_channel = guild_obj.get_channel(1418182282971320411)  # rapide (cache)
        if notify_channel is None:
            notify_channel = await client.fetch_channel(1418182282971320411)  # fallback
        if notify_channel is None:
            print("[on_ready] ⚠️ Impossible de trouver le salon de notification")
        else:
            print(f"[on_ready] Notifications XP → salon #{notify_channel.name} ({notify_channel.id})")
    except Exception as e:
        print(f"[on_ready] Erreur résolution notify_channel: {e}")

    # ➜ Lancer la boucle de polling
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
