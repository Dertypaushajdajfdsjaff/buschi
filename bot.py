import os
import time
import asyncio
import traceback
import copy
import subprocess
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

        # Für das erweiterte "Now Playing"-Widget:
        self.volume = 1.0
        self.repeat = False
        self.liked = set()  # Titel gemochter Songs (nur im RAM, pro Session)
        self.now_playing_message = None
        self.start_time = None
        self.paused_since = None
        self.paused_total = 0.0
        self.update_task = None
        self.disconnect_task = None
        self.ytdlp_process = None  # externer yt-dlp-Prozess, der Audiodaten an ffmpeg pipet


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
# Optionaler PO-Token-Provider (bgutil-ytdlp-pot-provider), siehe
# https://github.com/Brainicism/bgutil-ytdlp-pot-provider
# Nötig, weil YouTube Anfragen von Rechenzentrums-IPs (Railway etc.) inzwischen
# oft zusätzlich zu Cookies einen "Proof of Origin"-Token verlangt.
# POT_PROVIDER_URL z.B. "http://bgutil-provider.railway.internal:4416"
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL") or os.getenv("YTDLP_POT_PROVIDER_URL")
POT_PROVIDER_DISABLE_INNERTUBE = os.getenv("POT_PROVIDER_DISABLE_INNERTUBE", "0").lower() in {"1", "true", "yes", "on"}
YTDLP_JS_RUNTIME = os.getenv("YTDLP_JS_RUNTIME", "deno")
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
)

_BASE_YTDL_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "extractor_args": {
        "youtube": {
            # yt-dlp empfiehlt aktuell, den PO-Token-Provider den mweb-Client
            # für GVS-Requests versorgen zu lassen. web dient als Fallback.
            # (web allein braucht zusätzlich einen separaten Player-Token,
            # tv unterstützt Cookie-Login kaum noch zuverlässig.)
            "player_client": ["mweb", "web"],
        }
    },
    "http_headers": {
        "User-Agent": YTDLP_USER_AGENT,
    },
}

if POT_PROVIDER_URL:
    _pot_args = {"base_url": [POT_PROVIDER_URL]}
    if POT_PROVIDER_DISABLE_INNERTUBE:
        _pot_args["disable_innertube"] = ["1"]
    _BASE_YTDL_OPTIONS["extractor_args"]["youtubepot-bgutilhttp"] = _pot_args
    print(f"yt-dlp: PO-Token-Provider konfiguriert ({POT_PROVIDER_URL}).")
    if POT_PROVIDER_DISABLE_INNERTUBE:
        print("yt-dlp: PO-Token-Provider Legacy-Modus aktiv (disable_innertube=1).")
else:
    print("WARNUNG: POT_PROVIDER_URL ist nicht gesetzt -> kein externer PO-Token-Provider.")

if YTDLP_JS_RUNTIME:
    _BASE_YTDL_OPTIONS["js_runtimes"] = {YTDLP_JS_RUNTIME: {}}
    print(f"yt-dlp: JS-Runtime konfiguriert ({YTDLP_JS_RUNTIME}).")

if COOKIES_FILE and os.path.exists(COOKIES_FILE):
    _BASE_YTDL_OPTIONS["cookiefile"] = COOKIES_FILE
    _cookie_size = os.path.getsize(COOKIES_FILE)
    with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as _f:
        _cookie_lines = [l for l in _f if l.strip() and not l.startswith("#")]
    print(
        f"yt-dlp: Cookie-Datei geladen ({COOKIES_FILE}, "
        f"{_cookie_size} Bytes, {len(_cookie_lines)} Cookie-Einträge)."
    )
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as _cf:
            _cookie_header = _cf.readline().strip()
    except Exception:
        _cookie_header = ""

    if not _cookie_header.startswith(("# HTTP Cookie File", "# Netscape HTTP Cookie File")):
        print(
            "WARNUNG: COOKIES_FILE ist vermutlich nicht im Netscape/Mozilla-Format. "
            "Die erste Zeile sollte '# Netscape HTTP Cookie File' oder "
            "'# HTTP Cookie File' sein."
        )
    if _cookie_size < 500 or len(_cookie_lines) < 5:
        print(
            "WARNUNG: Cookie-Datei wirkt sehr klein/leer. "
            "Vermutlich kein vollständiger Export -> erneut exportieren."
        )
elif COOKIES_FILE:
    print(f"WARNUNG: COOKIES_FILE gesetzt, aber Datei nicht gefunden: {COOKIES_FILE}")
else:
    print("WARNUNG: COOKIES_FILE ist nicht gesetzt -> yt-dlp läuft ohne Cookies.")

# Optional: YTDLP_DEBUG=1 in Railway setzen, um bei Bot-Blocks die vollen
# yt-dlp-Debug-Infos (genutzter Client, PO-Token-Status, Cookie-Nutzung) im
# Log zu sehen. Für den Normalbetrieb aus, da es die Logs sehr voll macht.
if os.getenv("YTDLP_DEBUG", "0").lower() in {"1", "true", "yes", "on"}:
    _BASE_YTDL_OPTIONS["quiet"] = False
    _BASE_YTDL_OPTIONS["no_warnings"] = False
    _BASE_YTDL_OPTIONS["verbose"] = True
    print("yt-dlp: Debug-Modus AKTIV (YTDLP_DEBUG=1).")

YTDL_SEARCH_OPTIONS = copy.deepcopy(_BASE_YTDL_OPTIONS)
YTDL_SEARCH_OPTIONS.update({
    "default_search": "ytsearch",
    "extract_flat": True,
})

YTDL_AUDIO_OPTIONS = copy.deepcopy(_BASE_YTDL_OPTIONS)
YTDL_AUDIO_OPTIONS.update({
    # Fallback-Kette: falls kein reines Audio-Format verfügbar ist (kommt bei
    # manchen Videos/Clients vor -> "Requested format is not available"),
    # greift der Bot notfalls auf ein gemuxtes Video+Audio-Format zurück und
    # extrahiert daraus per ffmpeg (-vn) trotzdem nur den Ton.
    "format": "bestaudio[ext=m4a]/bestaudio/best[height<=480]/best",
})

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
    """Führt yt-dlp außerhalb des Event-Loops aus und versucht temporäre Fehler erneut.

    Der spezielle YouTube-Login/Bot-Block wird nicht sinnlos dreimal hintereinander
    wiederholt: Ohne neue Cookies/IP/PO-Token würde das nur Zeit verschwenden.
    """
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
            message = str(error)
            is_bot_block = (
                "Sign in to confirm" in message
                or "not a bot" in message.lower()
                or "confirm you're not a bot" in message.lower()
            )

            if is_bot_block:
                print(
                    f"[{label}] YouTube blockiert die Anfrage: "
                    "Sign in / bot verification erforderlich."
                )
                print(f"[{label}] Rohe yt-dlp-Fehlermeldung: {message}")
                raise

            print(
                f"[{label}] Versuch {attempt}/{retries} fehlgeschlagen: "
                f"{type(error).__name__}: {error}"
            )
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

            # yt-dlp hängt an info["url"] eine googlevideo.com-Adresse, die an
            # den PO-Token/Client-Kontext gebunden ist. Ohne die exakt
            # gleichen Request-Header (v.a. User-Agent) lehnt YouTubes CDN
            # den Zugriff durch ffmpeg mit 403 Forbidden ab.
            return {
                "url": info["url"],
                "title": info.get("title", "Unbekannter Song"),
                "webpage_url": info.get("webpage_url", webpage_url),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "http_headers": info.get("http_headers") or {},
            }

    return await _run_with_retries(extract, timeout=45, label="Audio-Extraktion")


def build_ytdlp_cli_args(webpage_url):
    """CLI-Argumente für den externen yt-dlp-Prozess, passend zu den
    Python-API-Optionen (_BASE_YTDL_OPTIONS), die auch für die Metadaten-
    Extraktion verwendet werden."""
    args = [
        "yt-dlp",
        "-f", YTDL_AUDIO_OPTIONS["format"],
        "-o", "-",
        "--no-playlist",
        "--geo-bypass",
        "--quiet",
        "--no-warnings",
        "--user-agent", YTDLP_USER_AGENT,
    ]

    player_clients = ",".join(
        _BASE_YTDL_OPTIONS["extractor_args"]["youtube"]["player_client"]
    )
    args += ["--extractor-args", f"youtube:player_client={player_clients}"]

    if POT_PROVIDER_URL:
        pot_arg = f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"
        if POT_PROVIDER_DISABLE_INNERTUBE:
            pot_arg += ";disable_innertube=1"
        args += ["--extractor-args", pot_arg]

    if YTDLP_JS_RUNTIME:
        args += ["--js-runtimes", YTDLP_JS_RUNTIME]

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        args += ["--cookies", COOKIES_FILE]

    args.append(webpage_url)
    return args


def start_ytdlp_stream(webpage_url):
    """Startet yt-dlp als eigenen Prozess, der die Audiodaten direkt an
    stdout streamt (statt ffmpeg die googlevideo.com-URL selbst abrufen zu
    lassen). Dadurch läuft der komplette YouTube-Request - inkl. Cookies,
    PO-Token und Headern - über yt-dlp's eigene, dafür konfigurierte
    HTTP-Session. Vermeidet 403 Forbidden durch abweichende Request-Header
    oder Verbindungspfade zwischen Extraktion und Wiedergabe."""
    args = build_ytdlp_cli_args(webpage_url)
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def stop_ytdlp_process(music):
    proc = music.ytdlp_process
    music.ytdlp_process = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def build_ffmpeg_options(http_headers):
    """Baut pro Song passende ffmpeg-Optionen inkl. der Request-Header, mit
    denen yt-dlp die Stream-URL geholt hat. Fehlen die, liefert YouTubes CDN
    für die googlevideo.com-URL oft ein 403 Forbidden."""
    headers = dict(http_headers or {})
    headers.setdefault("User-Agent", YTDLP_USER_AGENT)

    header_block = "".join(f"{key}: {value}\r\n" for key, value in headers.items())

    return {
        "before_options": (
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5 "
            f'-headers "{header_block}"'
        ),
        "options": "-vn",
    }


def is_youtube_bot_block(error):
    text = str(error).lower()
    return (
        "sign in to confirm" in text
        or "not a bot" in text
        or "confirm you're not a bot" in text
        or "use --cookies-from-browser" in text
    )


def cancel_disconnect_timer(music):
    if music.disconnect_task and not music.disconnect_task.done():
        music.disconnect_task.cancel()
    music.disconnect_task = None


def start_disconnect_timer(guild):
    music = get_music(guild.id)
    cancel_disconnect_timer(music)
    music.disconnect_task = asyncio.create_task(auto_disconnect_after_idle(guild))


def format_duration(seconds):
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def build_progress_bar(elapsed, duration, length=18):
    if not duration or duration <= 0:
        return "▬" * length
    frac = max(0.0, min(1.0, elapsed / duration))
    pos = int(frac * (length - 1))
    return "▬" * pos + "🔘" + "▬" * (length - 1 - pos)


def get_elapsed_seconds(music):
    if music.start_time is None:
        return 0
    if music.voice_client and music.voice_client.is_paused() and music.paused_since:
        return music.paused_since - music.start_time - music.paused_total
    return time.time() - music.start_time - music.paused_total


def create_music_embed(music, channel_name):
    song = music.current
    elapsed = get_elapsed_seconds(music)
    duration = song.get("duration")
    bar = build_progress_bar(elapsed, duration)
    time_text = f"{format_duration(elapsed)} / {format_duration(duration)}"
    liked = song["title"] in music.liked

    embed = discord.Embed(
        title="🎵  Now Playing",
        description=(
            f"**{song['title']}**\n\n"
            f"{bar}\n"
            f"`{time_text}`\n\n"
            f"🔊 **Voice:** {channel_name}\n"
            f"🔉 **Lautstärke:** {int(music.volume * 100)}%   •   "
            f"🔁 **Wiederholen:** {'An' if music.repeat else 'Aus'}   •   "
            f"{'❤️' if liked else '🤍'} **Geliked:** {'Ja' if liked else 'Nein'}"
        ),
        color=discord.Color.blurple(),
    )

    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])

    embed.set_footer(text="Music Bot • YouTube")
    return embed


async def now_playing_updater(guild_id):
    """Aktualisiert die 'Now Playing'-Nachricht alle paar Sekunden mit
    Fortschrittsbalken/Zeit, solange derselbe Song noch läuft."""
    music = get_music(guild_id)
    song_ref = music.current

    while (
        music.current is song_ref
        and music.now_playing_message is not None
        and music.voice_client is not None
        and music.voice_client.is_connected()
    ):
        try:
            embed = create_music_embed(music, music.voice_client.channel.name)
            await music.now_playing_message.edit(embed=embed)
        except discord.HTTPException:
            pass
        except Exception as error:
            print(f"Fehler beim Aktualisieren des Now-Playing-Widgets: {repr(error)}")
            break

        await asyncio.sleep(5)


# ============================================================
# AUTO-DISCONNECT NACH 15 MINUTEN OHNE MUSIK
# ============================================================
AUTO_DISCONNECT_DELAY = 15 * 60  # 15 Minuten


async def auto_disconnect_after_idle(guild):
    music = get_music(guild.id)

    try:
        print(f"[{guild.name}] Keine Musik mehr -> Auto-Disconnect in 15 Minuten.")

        await asyncio.sleep(AUTO_DISCONNECT_DELAY)

        # Prüfen, ob während der 15 Minuten wieder Musik gestartet wurde.
        if music.playing or music.starting_song or music.queue:
            print(f"[{guild.name}] Wieder Musik vorhanden -> Auto-Disconnect abgebrochen.")
            return

        vc = guild.voice_client

        if vc is not None and vc.is_connected():
            print(
                f"[{guild.name}] 15 Minuten ohne Musik -> "
                "Bot verlässt den Voice-Channel."
            )
            await vc.disconnect()

        music.voice_client = None
        music.current = None
        music.playing = False
        music.starting_song = False
        music.now_playing_message = None

    except asyncio.CancelledError:
        print(f"[{guild.name}] Auto-Disconnect Timer abgebrochen.")
        raise

    except Exception as error:
        print(
            f"[{guild.name}] Fehler beim Auto-Disconnect: "
            f"{type(error).__name__}: {error}"
        )

    finally:
        music.disconnect_task = None


# ============================================================
# SONG ABSPIELEN
# ============================================================
async def play_next(guild):
    music = get_music(guild.id)

    if music.voice_client is None or not music.voice_client.is_connected():
        music.playing = False
        music.current = None
        cancel_disconnect_timer(music)
        return

    if music.starting_song:
        return

    if not music.queue:
        music.playing = False
        music.current = None
        print(f"Queue von {guild.name} ist leer.")
        start_disconnect_timer(guild)
        return

    music.starting_song = True
    song = music.queue.popleft()
    music.current = song
    cancel_disconnect_timer(music)

    try:
        audio = await get_audio_url(song["webpage_url"])

        if audio.get("duration"):
            song["duration"] = audio["duration"]

        if music.voice_client is None or not music.voice_client.is_connected():
            raise RuntimeError("Der Voice-Client ist nicht mehr verbunden.")

        # Alten Pipe-Prozess (falls noch einer läuft) beenden, bevor ein
        # neuer gestartet wird.
        stop_ytdlp_process(music)

        proc = start_ytdlp_stream(song["webpage_url"])
        music.ytdlp_process = proc

        raw_source = discord.FFmpegPCMAudio(
            proc.stdout,
            pipe=True,
            options="-vn",
        )
        source = discord.PCMVolumeTransformer(raw_source, volume=music.volume)

        def after_play(error):
            stop_ytdlp_process(music)
            if error:
                print(f"Audio-Fehler bei '{song['title']}': {repr(error)}")
            try:
                asyncio.run_coroutine_threadsafe(
                    song_finished(guild),
                    bot.loop,
                )
            except Exception as callback_error:
                print(f"Fehler beim Song-Callback: {repr(callback_error)}")

        music.voice_client.play(source, after=after_play)
        music.playing = True
        music.history.appendleft(song)
        music.start_time = time.time()
        music.paused_since = None
        music.paused_total = 0.0

        if music.update_task and not music.update_task.done():
            music.update_task.cancel()

        if music.text_channel:
            embed = create_music_embed(music, music.voice_client.channel.name)
            music.now_playing_message = await music.text_channel.send(
                embed=embed,
                view=MusicView(guild.id),
            )
            music.update_task = asyncio.create_task(now_playing_updater(guild.id))

        print(f"Spiele: {song['title']}")

    except Exception as error:
        music.playing = False
        music.current = None
        stop_ytdlp_process(music)

        if is_youtube_bot_block(error):
            print(
                f"YouTube-Bot-Block bei '{song['title']}'. "
                "COOKIES_FILE/PO-Token/JS-Runtime prüfen."
            )
            print(f"Rohe yt-dlp-Fehlermeldung: {error}")
            error_message = (
                f"❌ YouTube blockiert **{song['title']}** auf dem Railway-Server.\n"
                "Bitte aktuelle YouTube-Cookies über `COOKIES_FILE` bereitstellen "
                "und – falls weiterhin nötig – einen PO-Token-Provider konfigurieren.\n"
                "Der Bot versucht automatisch den nächsten Song."
            )
        else:
            print(
                f"Fehler beim Abspielen von '{song['title']}': "
                f"{type(error).__name__}: {error}"
            )
            error_message = (
                f"❌ **{song['title']}** konnte nicht abgespielt werden. "
                "Der Bot versucht automatisch den nächsten Song."
            )

        if music.text_channel:
            try:
                await music.text_channel.send(error_message)
            except Exception:
                pass

        # Nicht an der Queue hängen bleiben: nächsten Song versuchen.
        if music.queue:
            await asyncio.sleep(0.5)
            await play_next(guild)
        else:
            start_disconnect_timer(guild)

    finally:
        music.starting_song = False


async def song_finished(guild):
    music = get_music(guild.id)
    finished_song = music.current
    music.playing = False
    music.current = None

    # Bei aktivem Repeat den gerade beendeten Song vorne wieder einreihen.
    if music.repeat and finished_song:
        music.queue.appendleft(finished_song)

    await asyncio.sleep(0.5)
    await play_next(guild)


# ============================================================
# MUSIC BUTTONS
# ============================================================
class MusicView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    # ---------- Reihe 1 ----------
    @discord.ui.button(label="Zurück", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        vc = interaction.guild.voice_client

        if vc is None or len(music.history) < 2:
            await interaction.response.send_message(
                "❌ Es gibt keinen vorherigen Song.",
                ephemeral=True,
            )
            return

        previous_song = music.history[1]
        music.queue.appendleft(previous_song)
        vc.stop()  # löst after_play -> song_finished -> play_next aus

        await interaction.response.send_message(
            f"⏮️ Spiele erneut: **{previous_song['title']}**",
            ephemeral=True,
        )

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        vc = interaction.guild.voice_client

        if vc is None:
            await interaction.response.send_message(
                "❌ Der Bot ist nicht im Voice-Channel.",
                ephemeral=True,
            )
            return

        if vc.is_playing():
            vc.pause()
            music.paused_since = time.time()
            button.label = "Fortsetzen"
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
            return

        if vc.is_paused():
            vc.resume()
            if music.paused_since:
                music.paused_total += time.time() - music.paused_since
                music.paused_since = None
            button.label = "Pause"
            button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
            return

        await interaction.response.send_message(
            "❌ Aktuell läuft kein Song.",
            ephemeral=True,
        )

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
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

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        music.queue.clear()

        if music.update_task and not music.update_task.done():
            music.update_task.cancel()

        if music.disconnect_task and not music.disconnect_task.done():
            music.disconnect_task.cancel()
            music.disconnect_task = None

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
        music.now_playing_message = None

        await interaction.response.send_message(
            "⏹️ Musik gestoppt und Queue geleert.",
            ephemeral=True,
        )

    @discord.ui.button(label="Like", emoji="🤍", style=discord.ButtonStyle.secondary, row=0)
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)

        if music.current is None:
            await interaction.response.send_message(
                "❌ Aktuell läuft kein Song.",
                ephemeral=True,
            )
            return

        title = music.current["title"]

        if title in music.liked:
            music.liked.discard(title)
            button.emoji = "🤍"
            message = f"💔 **{title}** aus den Likes entfernt."
        else:
            music.liked.add(title)
            button.emoji = "❤️"
            message = f"❤️ **{title}** geliked!"

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(message, ephemeral=True)

    # ---------- Reihe 2 ----------
    @discord.ui.button(label="Leiser", emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def volume_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        music.volume = max(0.0, round(music.volume - 0.1, 2))

        vc = interaction.guild.voice_client
        if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = music.volume

        await interaction.response.send_message(
            f"🔉 Lautstärke: **{int(music.volume * 100)}%**",
            ephemeral=True,
            delete_after=3,
        )

    @discord.ui.button(label="Lauter", emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def volume_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        music.volume = min(2.0, round(music.volume + 0.1, 2))

        vc = interaction.guild.voice_client
        if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = music.volume

        await interaction.response.send_message(
            f"🔊 Lautstärke: **{int(music.volume * 100)}%**",
            ephemeral=True,
            delete_after=3,
        )

    @discord.ui.button(label="Repeat: Aus", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        music.repeat = not music.repeat
        button.label = f"Repeat: {'An' if music.repeat else 'Aus'}"
        button.style = (
            discord.ButtonStyle.success if music.repeat else discord.ButtonStyle.secondary
        )
        await interaction.response.edit_message(view=self)


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
                    voice_channel.connect(reconnect=True),
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

    # Neue Musik angefordert -> Auto-Disconnect-Timer abbrechen.
    cancel_disconnect_timer(music)

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
    stop_ytdlp_process(music)

    if music.update_task and not music.update_task.done():
        music.update_task.cancel()

    cancel_disconnect_timer(music)

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
    music.now_playing_message = None

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

    music = get_music(interaction.guild.id)
    vc.pause()
    music.paused_since = time.time()
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

    music = get_music(interaction.guild.id)
    vc.resume()
    if music.paused_since:
        music.paused_total += time.time() - music.paused_since
        music.paused_since = None
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
    print(f"Auto-Disconnect: {AUTO_DISCONNECT_DELAY // 60} Minuten")
    print(f"YouTube-Cookies: {'AKTIV' if 'cookiefile' in _BASE_YTDL_OPTIONS else 'NICHT GESETZT'}")
    print(f"YouTube-PO-Token: {'AKTIV' if POT_PROVIDER_URL else 'NICHT GESETZT'}")
    print(f"YouTube-JS-Runtime: {'AKTIV' if YTDLP_JS_RUNTIME else 'STANDARD / NICHT EXPLIZIT GESETZT'}")
    if POT_PROVIDER_URL:
        print("YouTube-Setup: web + tv Client mit bgutil PO-Token-Provider")
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
