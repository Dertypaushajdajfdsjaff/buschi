import os
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ============================================================
# .ENV / RAILWAY
# ============================================================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError(
        "DISCORD_TOKEN wurde nicht gefunden. "
        "Lege die Variable in Railway an."
    )

# ============================================================
# EINSTELLUNGEN
# ============================================================
VOICE_CHANNEL_ID = 1534654223923282015
TEXT_CHANNEL_ID = 1534656181161693334
VOICE_COOLDOWN = 10 * 60

# Kanal-ID, in die das Audit-Log gepostet wird.
# <-- HIER die Channel-ID deines Log-Kanals eintragen.
AUDIT_LOG_CHANNEL_ID = 0

# ============================================================
# DISCORD INTENTS
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True
intents.messages = True
intents.message_content = True  # Nötig, um gelöschte/bearbeitete Inhalte zu loggen

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
# VOICE-PING SYSTEM
# ============================================================
last_ping = {}


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    if after.channel is None:
        return

    if after.channel.id != VOICE_CHANNEL_ID:
        return

    if before.channel == after.channel:
        return

    personen = [m for m in after.channel.members if not m.bot]

    print(f"{member.display_name} ist {after.channel.name} beigetreten.")

    if len(personen) > 1:
        print("Jemand ist bereits im Call -> kein Ping.")
        return

    jetzt = time.time()

    if member.id in last_ping:
        vergangen = jetzt - last_ping[member.id]

        if vergangen < VOICE_COOLDOWN:
            verbleibend = max(1, int((VOICE_COOLDOWN - vergangen) / 60))
            print(
                f"Cooldown aktiv für {member.display_name}. "
                f"Noch ca. {verbleibend} Minuten."
            )
            return

    text_channel = bot.get_channel(TEXT_CHANNEL_ID)

    if text_channel is None:
        print("Voice-Ping Textkanal wurde nicht gefunden.")
        return

    uhrzeit = datetime.now().strftime("%H:%M Uhr")

    embed = discord.Embed(
        title="🟢  Voice Aktiv",
        description=(
            f"🔊 **{member.display_name} ist im Call!**\n\n"
            f"👥 **Kanal:** {after.channel.name}\n"
            f"🕐 **Beigetreten:** {uhrzeit}\n\n"
            "────────────────────\n"
            "🎧 Der Voice-Chat ist jetzt aktiv!"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Voice Notification • 10 Minuten Cooldown")

    try:
        await text_channel.send(
            content="@everyone",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        last_ping[member.id] = jetzt
        print(f"@everyone wurde wegen {member.display_name} gepingt.")
    except Exception as error:
        print(f"Fehler beim Voice-Ping: {type(error).__name__}: {error}")


# ============================================================
# AUDIT LOG SYSTEM
# ============================================================
def get_audit_log_channel():
    """Gibt den konfigurierten Log-Kanal zurück, oder None, falls keine
    ID eingetragen wurde oder der Kanal (noch) nicht im Cache ist."""
    if not AUDIT_LOG_CHANNEL_ID:
        return None
    return bot.get_channel(AUDIT_LOG_CHANNEL_ID)


def _truncate(text, limit=1000):
    if text is None:
        return "*(kein Inhalt)*"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


async def find_audit_executor(guild, action, target_id=None, max_age=10):
    """Sucht im Server-Audit-Log nach dem Verursacher einer Aktion.

    Discord teilt bei den normalen Events (on_message_delete, on_guild_channel_update,
    ...) nicht mit, WER die Aktion ausgelöst hat - dafür muss man selbst im
    Audit-Log nachsehen. Es wird der neueste passende Eintrag verwendet, der
    nicht älter als `max_age` Sekunden ist und (falls angegeben) zum
    `target_id` passt. Ergebnis ist nicht zu 100% garantiert, aber die
    gängige und zuverlässigste Methode dafür.
    """
    if guild is None:
        return None

    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            alter = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if alter > max_age:
                break

            if target_id is not None:
                entry_target_id = getattr(entry.target, "id", None)
                if entry_target_id != target_id:
                    continue

            return entry.user

    except discord.Forbidden:
        print(
            "WARNUNG: Bot hat keine Berechtigung 'Audit-Log anzeigen' -> "
            "Verursacher von Aktionen kann nicht ermittelt werden."
        )
    except Exception as error:
        print(f"Fehler beim Lesen des Audit-Logs: {type(error).__name__}: {error}")

    return None


async def send_audit_embed(embed):
    log_channel = get_audit_log_channel()

    if log_channel is None:
        return

    try:
        await log_channel.send(embed=embed)
    except discord.Forbidden:
        print(
            "WARNUNG: Keine Berechtigung, in den Audit-Log-Kanal zu schreiben "
            f"(Kanal-ID {AUDIT_LOG_CHANNEL_ID})."
        )
    except Exception as error:
        print(f"Fehler beim Senden ins Audit-Log: {type(error).__name__}: {error}")


def _base_embed(title, color):
    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    return embed


# ---------- Nachricht gelöscht ----------
@bot.event
async def on_message_delete(message):
    if message.guild is None or (message.author and message.author.bot):
        return

    executor = await find_audit_executor(
        message.guild,
        discord.AuditLogAction.message_delete,
        target_id=message.author.id if message.author else None,
    )

    embed = _base_embed("🗑️ Nachricht gelöscht", discord.Color.red())
    embed.add_field(
        name="Autor",
        value=f"{message.author.mention} (`{message.author}`)" if message.author else "Unbekannt",
        inline=True,
    )
    embed.add_field(name="Kanal", value=message.channel.mention, inline=True)
    embed.add_field(
        name="Gelöscht von",
        value=f"{executor.mention} (`{executor}`)" if executor else "Unbekannt (evtl. selbst gelöscht)",
        inline=True,
    )
    embed.add_field(name="Nachricht", value=_truncate(message.content), inline=False)
    embed.set_footer(text=f"Nachrichten-ID: {message.id}")

    if message.author:
        embed.set_thumbnail(url=message.author.display_avatar.url)

    await send_audit_embed(embed)


# ---------- Nachricht bearbeitet ----------
@bot.event
async def on_message_edit(before, after):
    if before.guild is None or (before.author and before.author.bot):
        return

    if before.content == after.content:
        # z.B. nur ein Link-Embed wurde nachträglich geladen -> kein echter Edit
        return

    embed = _base_embed("✏️ Nachricht bearbeitet", discord.Color.orange())
    embed.add_field(
        name="Autor",
        value=f"{after.author.mention} (`{after.author}`)" if after.author else "Unbekannt",
        inline=True,
    )
    embed.add_field(name="Kanal", value=after.channel.mention, inline=True)
    embed.add_field(name="Link", value=f"[Zur Nachricht springen]({after.jump_url})", inline=True)
    embed.add_field(name="Vorher", value=_truncate(before.content), inline=False)
    embed.add_field(name="Nachher", value=_truncate(after.content), inline=False)
    embed.set_footer(text=f"Nachrichten-ID: {after.id}")

    if after.author:
        embed.set_thumbnail(url=after.author.display_avatar.url)

    await send_audit_embed(embed)


# ---------- Kanal erstellt ----------
@bot.event
async def on_guild_channel_create(channel):
    executor = await find_audit_executor(
        channel.guild, discord.AuditLogAction.channel_create, target_id=channel.id
    )

    embed = _base_embed("📁 Kanal erstellt", discord.Color.green())
    embed.add_field(name="Kanal", value=f"{channel.mention} (`{channel.name}`)", inline=True)
    embed.add_field(name="Typ", value=str(channel.type).replace("_", " ").title(), inline=True)
    embed.add_field(
        name="Erstellt von",
        value=f"{executor.mention} (`{executor}`)" if executor else "Unbekannt",
        inline=True,
    )
    embed.set_footer(text=f"Kanal-ID: {channel.id}")

    await send_audit_embed(embed)


# ---------- Kanal gelöscht ----------
@bot.event
async def on_guild_channel_delete(channel):
    executor = await find_audit_executor(
        channel.guild, discord.AuditLogAction.channel_delete, target_id=channel.id
    )

    embed = _base_embed("🗑️ Kanal gelöscht", discord.Color.dark_red())
    embed.add_field(name="Kanal", value=f"`#{channel.name}`", inline=True)
    embed.add_field(name="Typ", value=str(channel.type).replace("_", " ").title(), inline=True)
    embed.add_field(
        name="Gelöscht von",
        value=f"{executor.mention} (`{executor}`)" if executor else "Unbekannt",
        inline=True,
    )
    embed.set_footer(text=f"Kanal-ID: {channel.id}")

    await send_audit_embed(embed)


def _describe_channel_changes(before, after):
    """Vergleicht die gängigsten Attribute und gibt eine Liste von
    (Feldname, Vorher, Nachher) für alles zurück, was sich geändert hat."""
    changes = []

    if before.name != after.name:
        changes.append(("Name", f"`{before.name}`", f"`{after.name}`"))

    before_category = before.category.name if before.category else "Keine"
    after_category = after.category.name if after.category else "Keine"
    if before_category != after_category:
        changes.append(("Kategorie", before_category, after_category))

    if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
        if before.topic != after.topic:
            changes.append(("Thema", _truncate(before.topic, 200), _truncate(after.topic, 200)))
        if before.slowmode_delay != after.slowmode_delay:
            changes.append(("Slowmode", f"{before.slowmode_delay}s", f"{after.slowmode_delay}s"))
        if before.nsfw != after.nsfw:
            changes.append(("NSFW", str(before.nsfw), str(after.nsfw)))

    if isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
        if before.bitrate != after.bitrate:
            changes.append(("Bitrate", str(before.bitrate), str(after.bitrate)))
        if before.user_limit != after.user_limit:
            changes.append(("Nutzerlimit", str(before.user_limit), str(after.user_limit)))

    if before.overwrites != after.overwrites:
        changes.append(("Berechtigungen", "*(geändert)*", "*(geändert)*"))

    if before.position != after.position:
        changes.append(("Position", str(before.position), str(after.position)))

    return changes


# ---------- Kanal verändert ----------
@bot.event
async def on_guild_channel_update(before, after):
    changes = _describe_channel_changes(before, after)

    if not changes:
        return

    executor = await find_audit_executor(
        after.guild, discord.AuditLogAction.channel_update, target_id=after.id
    )

    embed = _base_embed("🔧 Kanal verändert", discord.Color.blurple())
    embed.add_field(name="Kanal", value=after.mention, inline=True)
    embed.add_field(
        name="Verändert von",
        value=f"{executor.mention} (`{executor}`)" if executor else "Unbekannt",
        inline=True,
    )

    for feld, vorher, nachher in changes:
        embed.add_field(name=feld, value=f"{vorher} ➜ {nachher}", inline=False)

    embed.set_footer(text=f"Kanal-ID: {after.id}")

    await send_audit_embed(embed)


# ============================================================
# /CLEAR
# ============================================================
@bot.tree.command(
    name="clear",
    description="Löscht eine Anzahl an Nachrichten in diesem Kanal.",
)
@app_commands.describe(anzahl="Wie viele Nachrichten gelöscht werden sollen (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, anzahl: app_commands.Range[int, 1, 100]):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Nur auf einem Server möglich.", ephemeral=True
        )
        return

    channel = interaction.channel

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "❌ In diesem Kanaltyp können keine Nachrichten gelöscht werden.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if not channel.permissions_for(bot_member).manage_messages:
        await interaction.response.send_message(
            "❌ Mir fehlt die Berechtigung **Nachrichten verwalten** in diesem Kanal.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await channel.purge(limit=anzahl)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Mir fehlt die Berechtigung, Nachrichten in diesem Kanal zu löschen.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"❌ Fehler beim Löschen: `{error}`",
            ephemeral=True,
        )
        return

    anzahl_geloescht = len(deleted)
    await interaction.followup.send(
        f"🧹 {anzahl_geloescht} "
        f"{'Nachricht wurde' if anzahl_geloescht == 1 else 'Nachrichten wurden'} gelöscht.",
        ephemeral=True,
    )


@clear.error
async def clear_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Du brauchst die Berechtigung **Nachrichten verwalten**, "
            "um diesen Befehl zu nutzen.",
            ephemeral=True,
        )
        return

    print(f"Fehler bei /clear: {type(error).__name__}: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(
            "❌ Es ist ein unerwarteter Fehler aufgetreten.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Es ist ein unerwarteter Fehler aufgetreten.", ephemeral=True
        )


# ============================================================
# BOT READY
# ============================================================
@bot.event
async def on_ready():
    print("====================================")
    print(f"Bot online: {bot.user}")
    print("Voice-Ping-System: AKTIV")
    print(
        "Audit-Log-System: "
        f"{'AKTIV -> Kanal-ID ' + str(AUDIT_LOG_CHANNEL_ID) if AUDIT_LOG_CHANNEL_ID else 'NICHT KONFIGURIERT (AUDIT_LOG_CHANNEL_ID setzen)'}"
    )
    print("====================================")

    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} Slash Commands synchronisiert.")
    except Exception as error:
        print(f"Fehler beim Synchronisieren: {type(error).__name__}: {error}")


# ============================================================
# BOT START
# ============================================================
bot.run(TOKEN)
