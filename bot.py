import os
import time
import asyncio
import traceback
from collections import deque
from datetime import datetime

import discord
from discord.ext import commands
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

# Optionaler Pfad zu einer cookies.txt (Netscape-Format), exportiert aus
# einem eingeloggten YouTube-Account. Hilft massiv gegen
# "Sign in to confirm you're not a bot" auf Cloud-IPs wie Railway.
# In Railway als Variable COOKIES_FILE setzen (z.B. "/app/cookies.txt")
# und die Datei per Volume/Secret bereitstellen.
COOKIES_FILE = os.getenv("COOKIES_FILE")

# ============================================================
# EINSTELLUNGEN
# ============================================================
VOICE_CHANNEL_ID = 1534654223923282015
TEXT_CHANNEL_ID = 1534656181161693334
VOICE_COOLDOWN = 10 * 60
MAX_HISTORY = 10

# ============================================================
# DISCORD INTENTS
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

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
# MUSIC DATEN
# ============================================================
class GuildMusic:
    def __init__(self):
        self.queue = deque()
        self.history = deque(maxlen=MAX_HISTORY)
        self.current = None
        self.voice_client = None
        self.text_channel = None
        self.playing = False
        self.starting_song = False


guild_music = {}


def get_music(guild_id):
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()
    return guild_music[guild_id]


# ============================================================
# YOUTUBE / YT-DLP
# ============================================================
# extractor_args mit "android"-Client + Cookie-Unterstützung reduzieren das
# Risiko von "Sign in to confirm you're not a bot" auf Cloud-IPs deutlich.
# 100%ig sicher ist es nicht, aber es ist aktuell der zuverlässigste
# Workaround ohne eigenen Proxy.
_BASE_YTDL_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    },
}

if COOKIES_FILE and os.path.exists(COOKIES_FILE):
    _BASE_YTDL_OPTIONS["cookiefile"] = COOKIES_FILE
    print(f"yt-dlp: Cookie-Datei geladen ({COOKIES_FILE}).")
elif COOKIES_FILE:
    print(f"WARNUNG: COOKIES_FILE gesetzt, aber Datei nicht gefunden: {COOKIES_FILE}")

YTDL_SEARCH_OPTIONS = {
    **_BASE_YTDL_OPTIONS,
    "default_search": "ytsearch",
    "extract_flat": True,
}

YTDL_AUDIO_OPTIONS = {
    **_BASE_YTDL_OPTIONS,
    "format": "bestaudio/best",
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}

YTDL_MAX_RETRIES = 3
YTDL_RETRY_DELAY = 2  # Sekunden


async def _run_with_retries(func, *, retries=YTDL_MAX_RETRIES, timeout=30, label="yt-dlp"):
    """Führt eine blockierende yt-dlp Funktion im Executor aus, mit Retries."""
    loop = asyncio.get_running_loop()
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, func),
                timeout=timeout,
            )
        except Exception as error:
            last_error = error
            print(f"[{label}] Versuch {attempt}/{retries} fehlgeschlagen: {repr(error)}")
            if attempt < retries:
                await asyncio.sleep(YTDL_RETRY_DELAY)

    raise last_error


async def search_youtube(query):
    def search():
        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS) as ydl:
            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False,
            )

            if not info or not info.get("entries"):
                return None

            return info["entries"][0]

    return await _run_with_retries(search, timeout=30, label="Suche")


async def get_audio_url(webpage_url):
    def extract():
        with yt_dlp.YoutubeDL(YTDL_AUDIO_OPTIONS) as ydl:
            info = ydl.extract_info(webpage_url, download=False)

            return {
                "url": info["url"],
                "title": info.get("title", "Unbekannter Song"),
                "webpage_url": info.get("webpage_url", webpage_url),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
            }

    return await _run_with_retries(extract, timeout=45, label="Audio-Extraktion")


# ============================================================
# MUSIC EMBED
# ============================================================
def create_music_embed(title, channel_name, thumbnail=None):
    embed = discord.Embed(
        title="🎵  Now Playing",
        description=(
            f"**{title}**\n\n"
            f"🔊 **Voice:** {channel_name}\n\n"
            "────────────────────\n"
            "🎧 Viel Spaß beim Hören!"
        ),
        color=discord.Color.blurple(),
    )

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    embed.set_footer(text="Music Bot • YouTube")
    return embed


# ============================================================
# SONG ABSPIELEN
# ============================================================
async def play_next(guild):
    music = get_music(guild.id)

    if music.voice_client is None or not music.voice_client.is_connected():
        music.playing = False
        music.current = None
        return

    if music.starting_song:
        return

    if not music.queue:
        music.playing = False
        music.current = None
        print(f"Queue von {guild.name} ist leer.")
        return

    music.starting_song = True
    song = music.queue.popleft()
    music.current = song

    try:
        audio = await get_audio_url(song["webpage_url"])

        if music.voice_client is None or not music.voice_client.is_connected():
            raise RuntimeError("Der Voice-Client ist nicht mehr verbunden.")

        source = discord.FFmpegPCMAudio(
            audio["url"],
            **FFMPEG_OPTIONS,
        )

        def after_play(error):
            if error:
                print(f"Audio-Fehler: {repr(error)}")

            asyncio.run_coroutine_threadsafe(
                song_finished(guild),
                bot.loop,
            )

        music.voice_client.play(source, after=after_play)
        music.playing = True
        music.history.appendleft(song)

        if music.text_channel:
            embed = create_music_embed(
                song["title"],
                music.voice_client.channel.name,
                song.get("thumbnail"),
            )
            await music.text_channel.send(
                embed=embed,
                view=MusicView(guild.id),
            )

        print(f"Spiele: {song['title']}")

    except Exception as error:
        music.playing = False
        music.current = None
        print("====================================")
        print("FEHLER BEIM ABSPIELEN:")
        print(f"Typ: {type(error).__name__}")
        print(f"Fehler: {repr(error)}")
        traceback.print_exc()
        print("====================================")

        if music.text_channel:
            try:
                await music.text_channel.send(
                    f"❌ Fehler beim Abspielen von **{song['title']}**:\n"
                    f"```{str(error)[:1500]}```"
                )
            except Exception:
                pass

        # Nächsten Song versuchen, falls einer in der Queue ist.
        if music.queue:
            await asyncio.sleep(0.5)
            await play_next(guild)
    finally:
        music.starting_song = False


async def song_finished(guild):
    music = get_music(guild.id)
    music.playing = False
    music.current = None
    await asyncio.sleep(0.5)
    await play_next(guild)


# ============================================================
# MUSIC BUTTONS
# ============================================================
class MusicView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client

        if vc is None:
            await interaction.response.send_message(
                "❌ Der Bot ist nicht im Voice-Channel.",
                ephemeral=True,
            )
            return

        if vc.is_playing():
            vc.pause()
            button.label = "Fortsetzen"
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
            return

        if vc.is_paused():
            vc.resume()
            button.label = "Pause"
            button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
            return

        await interaction.response.send_message(
            "❌ Aktuell läuft kein Song.",
            ephemeral=True,
        )

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client

        if vc is None or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message(
                "❌ Es läuft gerade kein Song.",
                ephemeral=True,
            )
            return

        vc.stop()
        await interaction.response.send_message(
            "⏭️ Song übersprungen!",
            ephemeral=True,
        )

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        music.queue.clear()

        vc = interaction.guild.voice_client

        if vc is not None:
            try:
                vc.stop()
            except Exception:
                pass

            try:
                await vc.disconnect()
            except Exception:
                pass

        music.voice_client = None
        music.current = None
        music.playing = False
        music.starting_song = False

        await interaction.response.send_message(
            "⏹️ Musik gestoppt und Queue geleert.",
            ephemeral=True,
        )


# ============================================================
# VOICE JOIN HELFER (mit Retries)
# ============================================================
async def connect_voice(interaction, voice_channel, retries=2):
    """Verbindet mit einem Voice-Channel und gibt (vc, error) zurück.
    Bei RuntimeError/Timeout wird ein zweiter Versuch gemacht, da das oft
    an einem einmaligen UDP-Handshake-Hänger liegt."""
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            vc = interaction.guild.voice_client

            if vc is not None and not vc.is_connected():
                print("Alter Voice-Client gefunden -> trenne ihn.")
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None

            if vc is None:
                print(f"Verbinde mit Voice-Channel: {voice_channel.name} (Versuch {attempt}/{retries})")
                vc = await asyncio.wait_for(
                    voice_channel.connect(reconnect=True, self_deaf=True),
                    timeout=20,
                )
            elif vc.channel != voice_channel:
                print(f"Wechsle Voice-Channel nach: {voice_channel.name}")
                await asyncio.wait_for(
                    vc.move_to(voice_channel),
                    timeout=20,
                )

            return vc, None

        except Exception as error:
            last_error = error
            print(f"Voice-Connect Versuch {attempt}/{retries} fehlgeschlagen: {type(error).__name__}: {repr(error)}")
            traceback.print_exc()

            # Hängenden Client vor dem nächsten Versuch aufräumen.
            try:
                stale_vc = interaction.guild.voice_client
                if stale_vc is not None:
                    await stale_vc.disconnect(force=True)
            except Exception:
                pass

            if attempt < retries:
                await asyncio.sleep(2)

    return None, last_error


# ============================================================
# /PLAY
# ============================================================
@bot.tree.command(name="play", description="Spielt einen Song von YouTube ab.")
@app_commands.describe(song="Name des Songs oder YouTube-Link")
async def play(interaction: discord.Interaction, song: str):
    # Sofort bestätigen, damit Discord nicht wegen Timeout abbricht.
    await interaction.response.defer()

    if interaction.guild is None:
        await interaction.followup.send(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    if interaction.user.voice is None:
        await interaction.followup.send(
            "❌ Du musst zuerst in einem Voice-Channel sein."
        )
        return

    voice_channel = interaction.user.voice.channel
    music = get_music(interaction.guild.id)
    music.text_channel = interaction.channel

    # ========================================================
    # SONG SUCHEN
    # ========================================================
    try:
        result = await search_youtube(song)
    except asyncio.TimeoutError:
        print("YouTube-Suche Timeout (alle Versuche)")
        await interaction.followup.send(
            "❌ Die YouTube-Suche hat zu lange gedauert. Bitte versuche es erneut."
        )
        return
    except Exception as error:
        print("====================================")
        print("YOUTUBE FEHLER:")
        print(f"Typ: {type(error).__name__}")
        print(f"Fehler: {repr(error)}")
        traceback.print_exc()
        print("====================================")

        fehlertext = str(error)
        if "Sign in to confirm" in fehlertext or "bot" in fehlertext.lower():
            await interaction.followup.send(
                "❌ YouTube blockiert die Suche von diesem Server aus "
                "(\"Sign in to confirm you're not a bot\"). Das ist ein "
                "bekanntes Problem bei Cloud-Hostern wie Railway. Siehe "
                "Railway-Logs für Details – ggf. wird ein Cookie-Login "
                "(COOKIES_FILE) benötigt."
            )
        else:
            await interaction.followup.send(
                f"❌ YouTube-Fehler:\n```{fehlertext[:1800]}```"
            )
        return

    if result is None:
        await interaction.followup.send("❌ Ich konnte den Song nicht finden.")
        return

    webpage_url = result.get("webpage_url") or result.get("url")

    if not webpage_url:
        await interaction.followup.send(
            "❌ YouTube hat keinen gültigen Link für diesen Song geliefert."
        )
        return

    song_data = {
        "title": result.get("title", "Unbekannter Song"),
        "webpage_url": webpage_url,
        "thumbnail": result.get("thumbnail"),
        "duration": result.get("duration"),
    }

    # ========================================================
    # VOICE JOIN / RECONNECT
    # ========================================================
    vc, voice_error = await connect_voice(interaction, voice_channel)

    if voice_error is not None:
        error = voice_error

        if isinstance(error, discord.Forbidden):
            await interaction.followup.send(
                "❌ Ich darf diesem Voice-Channel nicht beitreten. "
                "Prüfe die Rechte **Kanal ansehen**, **Verbinden** und **Sprechen**."
            )
        elif isinstance(error, asyncio.TimeoutError):
            await interaction.followup.send(
                "❌ Der Voice-Connect hat mehrfach zu lange gedauert.\n"
                "Das deutet stark auf ein **UDP-Blocking** durch den Hoster "
                "(z.B. Railway) hin – der Websocket-Teil klappt, aber der "
                "eigentliche Audio-Handshake (UDP) nicht. Prüfe die Railway-"
                "Netzwerkeinstellungen oder wechsle zu einem Hoster mit "
                "offenem UDP-Traffic."
            )
        elif isinstance(error, discord.ClientException):
            await interaction.followup.send(
                "❌ Discord hat die Voice-Verbindung abgelehnt. "
                "Details wurden in Railway protokolliert."
            )
        else:
            await interaction.followup.send(
                "❌ Ich konnte dem Voice-Channel nicht beitreten.\n"
                f"Fehler: `{type(error).__name__}: {str(error)[:300]}`"
            )
        return

    music.voice_client = vc

    # ========================================================
    # SONG IN QUEUE
    # ========================================================
    music.queue.append(song_data)

    if not music.playing and not music.starting_song:
        await interaction.followup.send(
            f"🎵 **{song_data['title']}** wird abgespielt."
        )
        await play_next(interaction.guild)
    else:
        position = len(music.queue)
        await interaction.followup.send(
            f"✅ **{song_data['title']}** wurde zur Queue hinzugefügt.\n"
            f"📋 Position: **{position}**"
        )


# ============================================================
# /SKIP
# ============================================================
@bot.tree.command(name="skip", description="Überspringt den aktuellen Song.")
async def skip(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    vc = interaction.guild.voice_client

    if vc is None or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("❌ Es läuft gerade kein Song.")
        return

    vc.stop()
    await interaction.response.send_message("⏭️ Song übersprungen!")


# ============================================================
# /STOP
# ============================================================
@bot.tree.command(name="stop", description="Stoppt die Musik und leert die Queue.")
async def stop(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    music = get_music(interaction.guild.id)
    music.queue.clear()

    vc = interaction.guild.voice_client

    if vc:
        try:
            vc.stop()
        except Exception:
            pass

        try:
            await vc.disconnect()
        except Exception:
            pass

    music.voice_client = None
    music.current = None
    music.playing = False
    music.starting_song = False

    await interaction.response.send_message(
        "⏹️ Musik gestoppt und Queue geleert."
    )


# ============================================================
# /QUEUE
# ============================================================
@bot.tree.command(name="queue", description="Zeigt die aktuelle Musik-Queue.")
async def queue(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    music = get_music(interaction.guild.id)

    if not music.queue:
        await interaction.response.send_message("📋 Die Queue ist leer.")
        return

    text = "\n".join(
        f"**{index}.** {song['title']}"
        for index, song in enumerate(music.queue, start=1)
    )

    embed = discord.Embed(
        title="📋  Musik Queue",
        description=text,
        color=discord.Color.blurple(),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# /HISTORY
# ============================================================
@bot.tree.command(name="history", description="Zeigt die letzten 10 gespielten Songs.")
async def history(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    music = get_music(interaction.guild.id)

    if not music.history:
        await interaction.response.send_message(
            "📜 Es wurden noch keine Songs gespielt."
        )
        return

    text = "\n".join(
        f"**{index}.** {song['title']}"
        for index, song in enumerate(music.history, start=1)
    )

    embed = discord.Embed(
        title="📜  Letzte 10 Songs",
        description=text,
        color=discord.Color.green(),
    )

    await interaction.response.send_message(embed=embed)


# ============================================================
# /PAUSE
# ============================================================
@bot.tree.command(name="pause", description="Pausiert die aktuelle Musik.")
async def pause(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    vc = interaction.guild.voice_client

    if vc is None or not vc.is_playing():
        await interaction.response.send_message("❌ Es läuft gerade kein Song.")
        return

    vc.pause()
    await interaction.response.send_message("⏸️ Musik pausiert.")


# ============================================================
# /RESUME
# ============================================================
@bot.tree.command(name="resume", description="Setzt die Musik fort.")
async def resume(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    vc = interaction.guild.voice_client

    if vc is None or not vc.is_paused():
        await interaction.response.send_message("❌ Die Musik ist nicht pausiert.")
        return

    vc.resume()
    await interaction.response.send_message("▶️ Musik läuft weiter.")


# ============================================================
# BOT READY
# ============================================================
@bot.event
async def on_ready():
    print("====================================")
    print(f"Bot online: {bot.user}")
    print("Voice-Ping-System: AKTIV")
    print("Music-System: AKTIV")
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
