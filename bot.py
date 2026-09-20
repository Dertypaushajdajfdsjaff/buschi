import asyncio
import functools
import os
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp

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
TEXT_CHANNEL_ID = 1548313708961202226
VOICE_COOLDOWN = 10 * 60

# Zeitzone für alle angezeigten Uhrzeiten (Server, z.B. Railway, läuft
# in der Regel in UTC -> ohne diese Umrechnung wäre die Uhrzeit falsch).
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")

# Kanal-ID, in die das Audit-Log gepostet wird.
# <-- HIER die Channel-ID deines Log-Kanals eintragen.
AUDIT_LOG_CHANNEL_ID = 1534701792061816872

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

    uhrzeit = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M Uhr")

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
# MUSIC BOT SYSTEM
# ============================================================
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

AUTO_DISCONNECT_SECONDS = 5 * 60  # Nach 5 Minuten Inaktivität den Voice-Channel verlassen
PROGRESS_UPDATE_SECONDS = 10  # Wie oft die "Now Playing"-Nachricht aktualisiert wird

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Track:
    """Repräsentiert einen einzelnen Song in der Warteschlange."""

    def __init__(self, data, requester):
        self.title = data.get("title") or "Unbekannter Titel"
        self.stream_url = data.get("url")
        self.webpage_url = data.get("webpage_url")
        self.duration = data.get("duration") or 0
        self.thumbnail = data.get("thumbnail")
        self.requester = requester
        self.liked = False


async def extract_track(query, requester):
    """Lädt Song-Infos von YouTube (blockierend), daher im Executor ausgeführt."""
    partial = functools.partial(ytdl.extract_info, query, download=False)
    data = await bot.loop.run_in_executor(None, partial)

    if data is None:
        raise ValueError("Keine Ergebnisse gefunden.")

    if "entries" in data:
        entries = [e for e in data["entries"] if e is not None]
        if not entries:
            raise ValueError("Keine Ergebnisse gefunden.")
        data = entries[0]

    return Track(data, requester)


class GuildMusicState:
    """Hält den kompletten Musik-Zustand für genau einen Server (Guild)."""

    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = deque()
        self.history = deque(maxlen=10)
        self.voice_client = None
        self.current = None
        self.volume = 0.5  # 0.0 - 2.0 (also 0% - 200%)
        self.loop_current = False
        self.text_channel = None
        self.now_playing_message = None
        self.play_started_at = None
        self.paused_at_elapsed = 0
        self.update_task = None
        self.disconnect_task = None

    def is_active(self):
        return self.voice_client is not None and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        )


music_states = {}


def get_music_state(guild_id):
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


def format_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_progress_bar(elapsed, total, length=18):
    if not total:
        return "▬" * length
    fraction = max(0.0, min(1.0, elapsed / total))
    pos = int(fraction * (length - 1))
    return "".join("🔘" if i == pos else "▬" for i in range(length))


def get_elapsed(state):
    if state.current is None:
        return 0
    if state.voice_client and state.voice_client.is_paused():
        return state.paused_at_elapsed
    if state.play_started_at is None:
        return state.paused_at_elapsed
    return state.paused_at_elapsed + (time.time() - state.play_started_at)


def build_now_playing_embed(state):
    track = state.current
    elapsed = get_elapsed(state)
    bar = build_progress_bar(elapsed, track.duration)

    vc = state.voice_client
    if vc and vc.is_paused():
        status = "😴 Pausiert"
    elif vc and vc.is_playing():
        status = "🔊 Spielt"
    else:
        status = "❌ Gestoppt"

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=(
            f"**{track.title}**\n\n"
            f"{bar}\n"
            f"`{format_duration(elapsed)} / {format_duration(track.duration)}`"
        ),
        color=discord.Color.blurple(),
        url=track.webpage_url,
    )
    embed.add_field(name="Voice", value=status, inline=True)
    embed.add_field(name="Lautstärke", value=f"{int(round(state.volume * 100))}%", inline=True)
    embed.add_field(
        name="Wiederholen",
        value="An 🔁" if state.loop_current else "Aus",
        inline=True,
    )
    embed.add_field(name="Geliked", value="Ja ❤️" if track.liked else "Nein", inline=True)

    if track.requester:
        embed.add_field(name="Angefragt von", value=track.requester.mention, inline=True)
    if state.queue:
        embed.add_field(name="Als Nächstes", value=state.queue[0].title, inline=True)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    embed.set_footer(text="Music Bot • YouTube")
    return embed


def cancel_update_task(state):
    if state.update_task is not None:
        state.update_task.cancel()
        state.update_task = None


def start_progress_updater(state):
    cancel_update_task(state)

    @tasks.loop(seconds=PROGRESS_UPDATE_SECONDS)
    async def updater():
        if state.current is None or state.now_playing_message is None:
            updater.stop()
            return
        try:
            await state.now_playing_message.edit(embed=build_now_playing_embed(state))
        except (discord.NotFound, discord.HTTPException):
            pass

    state.update_task = updater
    updater.start()


def cancel_auto_disconnect(state):
    if state.disconnect_task is not None:
        state.disconnect_task.cancel()
        state.disconnect_task = None


def schedule_auto_disconnect(state):
    cancel_auto_disconnect(state)

    async def _disconnect_later():
        await asyncio.sleep(AUTO_DISCONNECT_SECONDS)
        if state.voice_client and not state.is_active() and not state.queue:
            try:
                await state.voice_client.disconnect(force=True)
            except Exception:
                pass
            state.voice_client = None

    state.disconnect_task = bot.loop.create_task(_disconnect_later())


async def send_or_refresh_now_playing(state):
    if state.now_playing_message is not None:
        try:
            await state.now_playing_message.edit(view=None)
        except (discord.NotFound, discord.HTTPException):
            pass

    embed = build_now_playing_embed(state)
    view = MusicControlView(state.guild_id)

    try:
        state.now_playing_message = await state.text_channel.send(embed=embed, view=view)
    except discord.HTTPException as error:
        print(f"Fehler beim Senden der Now-Playing-Nachricht: {error}")


async def play_next_track(state):
    """Startet den nächsten Song aus der Warteschlange (oder wiederholt den aktuellen)."""
    if state.voice_client is None or not state.voice_client.is_connected():
        return

    if state.loop_current and state.current is not None:
        next_track = state.current
    else:
        if state.current is not None:
            state.history.append(state.current)

        if not state.queue:
            state.current = None
            cancel_update_task(state)
            if state.now_playing_message is not None:
                embed = discord.Embed(
                    title="📭 Warteschlange beendet",
                    description="Füge mit `/play` neue Songs hinzu.",
                    color=discord.Color.dark_grey(),
                )
                try:
                    await state.now_playing_message.edit(embed=embed, view=None)
                except (discord.NotFound, discord.HTTPException):
                    pass
            schedule_auto_disconnect(state)
            return

        next_track = state.queue.popleft()

    state.current = next_track

    try:
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(next_track.stream_url, **FFMPEG_OPTIONS),
            volume=state.volume,
        )
    except Exception as error:
        print(f"Fehler beim Erstellen der Audio-Quelle: {error}")
        await play_next_track(state)
        return

    def after_playback(error):
        if error:
            print(f"Player-Fehler: {error}")
        asyncio.run_coroutine_threadsafe(play_next_track(state), bot.loop)

    state.voice_client.play(source, after=after_playback)
    state.play_started_at = time.time()
    state.paused_at_elapsed = 0

    await send_or_refresh_now_playing(state)
    start_progress_updater(state)


class MusicControlView(discord.ui.View):
    """Buttons unter der 'Now Playing'-Nachricht, analog zu deinem Screenshot."""

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def get_state(self):
        return get_music_state(self.guild_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state = self.get_state()
        if state.voice_client is None:
            await interaction.response.send_message(
                "❌ Der Bot ist gerade in keinem Voice-Channel.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Zurück", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        if not state.history:
            await interaction.response.send_message("❌ Es gibt keinen vorherigen Song.", ephemeral=True)
            return

        previous_track = state.history.pop()
        if state.current is not None:
            state.queue.appendleft(state.current)
        state.queue.appendleft(previous_track)

        await interaction.response.defer()
        state.voice_client.stop()  # löst über 'after' automatisch play_next_track aus

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        vc = state.voice_client

        if vc.is_playing():
            vc.pause()
            state.paused_at_elapsed = get_elapsed(state)
            state.play_started_at = None
            button.label, button.emoji = "Weiter", "▶️"
        elif vc.is_paused():
            vc.resume()
            state.play_started_at = time.time()
            button.label, button.emoji = "Pause", "⏸️"
        else:
            await interaction.response.send_message("❌ Es läuft gerade nichts.", ephemeral=True)
            return

        await interaction.response.edit_message(embed=build_now_playing_embed(state), view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        if not state.is_active():
            await interaction.response.send_message("❌ Es läuft gerade nichts.", ephemeral=True)
            return
        await interaction.response.defer()
        state.voice_client.stop()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        state.queue.clear()
        state.history.clear()
        state.loop_current = False
        cancel_update_task(state)
        cancel_auto_disconnect(state)

        if state.voice_client is not None:
            try:
                state.voice_client.stop()
                await state.voice_client.disconnect(force=True)
            except Exception:
                pass

        state.voice_client = None
        state.current = None

        embed = discord.Embed(
            title="⏹️ Wiedergabe gestoppt",
            description="Die Warteschlange wurde geleert und der Voice-Channel verlassen.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Like", emoji="🤍", style=discord.ButtonStyle.secondary, row=1)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        if state.current is None:
            await interaction.response.send_message("❌ Es läuft gerade nichts.", ephemeral=True)
            return
        state.current.liked = not state.current.liked
        button.emoji = "❤️" if state.current.liked else "🤍"
        await interaction.response.edit_message(embed=build_now_playing_embed(state), view=self)

    @discord.ui.button(label="Leiser", emoji="🔉", style=discord.ButtonStyle.secondary, row=2)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        state.volume = max(0.0, round(state.volume - 0.1, 2))
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        await interaction.response.edit_message(embed=build_now_playing_embed(state), view=self)

    @discord.ui.button(label="Lauter", emoji="🔊", style=discord.ButtonStyle.secondary, row=2)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        state.volume = min(2.0, round(state.volume + 0.1, 2))
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        await interaction.response.edit_message(embed=build_now_playing_embed(state), view=self)

    @discord.ui.button(label="Repeat: Aus", emoji="🔁", style=discord.ButtonStyle.secondary, row=2)
    async def repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.get_state()
        state.loop_current = not state.loop_current
        button.label = f"Repeat: {'An' if state.loop_current else 'Aus'}"
        button.style = discord.ButtonStyle.success if state.loop_current else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=build_now_playing_embed(state), view=self)


@bot.tree.command(name="play", description="Spielt einen Song ab oder fügt ihn zur Warteschlange hinzu.")
@app_commands.describe(song="Songname (Suche) oder YouTube-Link")
async def play(interaction: discord.Interaction, song: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(
            "❌ Du musst in einem Voice-Channel sein, um Musik abzuspielen.", ephemeral=True
        )
        return

    await interaction.response.defer()

    state = get_music_state(interaction.guild.id)
    state.text_channel = interaction.channel
    voice_channel = member.voice.channel

    if state.voice_client is None or not state.voice_client.is_connected():
        try:
            state.voice_client = await voice_channel.connect()
        except Exception as error:
            await interaction.followup.send(f"❌ Konnte dem Voice-Channel nicht beitreten: `{error}`")
            return
    elif state.voice_client.channel.id != voice_channel.id:
        await state.voice_client.move_to(voice_channel)

    cancel_auto_disconnect(state)

    try:
        track = await extract_track(song, member)
    except Exception as error:
        await interaction.followup.send(f"❌ Song konnte nicht geladen werden: `{error}`")
        return

    state.queue.append(track)

    if state.is_active():
        await interaction.followup.send(
            f"➕ **{track.title}** wurde zur Warteschlange hinzugefügt. (Position {len(state.queue)})"
        )
    else:
        await interaction.followup.send(f"▶️ Starte **{track.title}**...")
        await play_next_track(state)


@bot.tree.command(name="skip", description="Überspringt den aktuellen Song.")
async def skip_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    if not state.is_active():
        await interaction.response.send_message("❌ Es läuft gerade nichts.", ephemeral=True)
        return
    state.voice_client.stop()
    await interaction.response.send_message("⏭️ Song übersprungen.", ephemeral=True)


@bot.tree.command(name="stop", description="Stoppt die Wiedergabe, leert die Warteschlange und verlässt den Voice-Channel.")
async def stop_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    state.queue.clear()
    state.history.clear()
    state.loop_current = False
    cancel_update_task(state)
    cancel_auto_disconnect(state)

    if state.voice_client is not None:
        try:
            state.voice_client.stop()
            await state.voice_client.disconnect(force=True)
        except Exception:
            pass

    state.voice_client = None
    state.current = None
    await interaction.response.send_message("⏹️ Wiedergabe gestoppt und Voice-Channel verlassen.", ephemeral=True)


@bot.tree.command(name="queue", description="Zeigt die aktuelle Warteschlange an.")
async def queue_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    if state.current is None and not state.queue:
        await interaction.response.send_message("📭 Die Warteschlange ist leer.", ephemeral=True)
        return

    lines = []
    if state.current:
        lines.append(f"**Gerade läuft:** {state.current.title}")
    for i, track in enumerate(state.queue, start=1):
        lines.append(f"`{i}.` {track.title}")

    embed = discord.Embed(
        title="📜 Warteschlange",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="volume", description="Stellt die Lautstärke ein (0-200%).")
@app_commands.describe(prozent="Lautstärke in Prozent, z.B. 50")
async def volume_command(interaction: discord.Interaction, prozent: app_commands.Range[int, 0, 200]):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    state.volume = prozent / 100
    if state.voice_client and state.voice_client.source:
        state.voice_client.source.volume = state.volume
    await interaction.response.send_message(f"🔊 Lautstärke auf {prozent}% gesetzt.", ephemeral=True)


@bot.listen("on_voice_state_update")
async def music_auto_leave(member, before, after):
    """Verlässt den Voice-Channel automatisch, wenn keine echten Nutzer mehr drin sind."""
    if member.guild is None:
        return

    state = music_states.get(member.guild.id)
    if state is None or state.voice_client is None:
        return

    voice_channel = state.voice_client.channel
    if voice_channel is None:
        return

    non_bot_members = [m for m in voice_channel.members if not m.bot]
    if non_bot_members:
        return

    try:
        state.voice_client.stop()
        await state.voice_client.disconnect(force=True)
    except Exception:
        pass

    state.voice_client = None
    state.queue.clear()
    state.current = None
    cancel_update_task(state)
    cancel_auto_disconnect(state)


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
