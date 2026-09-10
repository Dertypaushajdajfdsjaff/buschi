import os
import time
import asyncio
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

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# VOICE-PING SYSTEM
# ============================================================

last_ping = {}


@bot.event
async def on_voice_state_update(member, before, after):

    # Bots ignorieren
    if member.bot:
        return

    # Kein Voice-Channel
    if after.channel is None:
        return

    # Nur unseren gewünschten Voice-Channel überwachen
    if after.channel.id != VOICE_CHANNEL_ID:
        return

    # Nur tatsächlichen Channel-Wechsel erkennen
    if before.channel == after.channel:
        return

    personen = [
        m for m in after.channel.members
        if not m.bot
    ]

    # Wenn mehr als eine Person drin ist -> kein Ping
    if len(personen) > 1:
        print(
            f"{member.display_name} ist beigetreten, "
            f"aber jemand ist bereits im Call."
        )
        return

    jetzt = time.time()

    # 10-Minuten-Cooldown
    if member.id in last_ping:
        vergangen = jetzt - last_ping[member.id]

        if vergangen < VOICE_COOLDOWN:
            verbleibend = int(
                (VOICE_COOLDOWN - vergangen) / 60
            )

            print(
                f"Cooldown für {member.display_name} aktiv. "
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
        color=discord.Color.green()
    )

    embed.set_footer(
        text="Voice Notification • 10 Minuten Cooldown"
    )

    try:
        await text_channel.send(
            content="@everyone",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=True
            )
        )

        last_ping[member.id] = jetzt

        print(
            f"@everyone wurde wegen "
            f"{member.display_name} gepingt."
        )

    except Exception as error:
        print(f"Fehler beim Voice-Ping: {repr(error)}")


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

YTDL_SEARCH_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": True,
    "default_search": "ytsearch",
}


YTDL_AUDIO_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
}


FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn"
}


async def search_youtube(query):

    loop = asyncio.get_running_loop()

    def search():

        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS) as ydl:

            info = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False
            )

            if not info:
                return None

            entries = info.get("entries")

            if not entries:
                return None

            result = entries[0]

            if not result:
                return None

            webpage_url = (
                result.get("webpage_url")
                or result.get("url")
            )

            if not webpage_url:
                return None

            return {
                "title": result.get(
                    "title",
                    "Unbekannter Song"
                ),
                "webpage_url": webpage_url,
                "thumbnail": result.get("thumbnail"),
                "duration": result.get("duration"),
            }

    return await asyncio.wait_for(
        loop.run_in_executor(None, search),
        timeout=30
    )


async def get_audio_url(webpage_url):

    loop = asyncio.get_running_loop()

    def extract():

        with yt_dlp.YoutubeDL(YTDL_AUDIO_OPTIONS) as ydl:

            info = ydl.extract_info(
                webpage_url,
                download=False
            )

            if not info:
                raise RuntimeError(
                    "YouTube hat keine Informationen geliefert."
                )

            audio_url = info.get("url")

            if not audio_url:
                raise RuntimeError(
                    "Keine Audio-URL von YouTube erhalten."
                )

            return {
                "url": audio_url,
                "title": info.get(
                    "title",
                    "Unbekannter Song"
                ),
                "webpage_url": info.get(
                    "webpage_url",
                    webpage_url
                ),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
            }

    return await asyncio.wait_for(
        loop.run_in_executor(None, extract),
        timeout=45
    )


# ============================================================
# MUSIC EMBED
# ============================================================

def create_music_embed(
    title,
    channel_name,
    thumbnail=None
):

    embed = discord.Embed(
        title="🎵  Now Playing",
        description=(
            f"**{title}**\n\n"
            f"🔊 **Voice:** {channel_name}\n\n"
            "────────────────────\n"
            "🎧 Viel Spaß beim Hören!"
        ),
        color=discord.Color.blurple()
    )

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    embed.set_footer(
        text="Music Bot • YouTube"
    )

    return embed


# ============================================================
# MUSIC BUTTONS
# ============================================================

class MusicView(discord.ui.View):

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(
        label="Pause",
        emoji="⏸️",
        style=discord.ButtonStyle.primary
    )
    async def pause_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        vc = interaction.guild.voice_client

        if vc is None:
            await interaction.response.send_message(
                "❌ Der Bot ist nicht im Voice-Channel.",
                ephemeral=True
            )
            return

        if vc.is_playing():

            vc.pause()

            button.label = "Fortsetzen"
            button.emoji = "▶️"

            await interaction.response.edit_message(
                view=self
            )

            return

        if vc.is_paused():

            vc.resume()

            button.label = "Pause"
            button.emoji = "⏸️"

            await interaction.response.edit_message(
                view=self
            )

            return

        await interaction.response.send_message(
            "❌ Aktuell läuft kein Song.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Skip",
        emoji="⏭️",
        style=discord.ButtonStyle.secondary
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        vc = interaction.guild.voice_client

        if vc is None or not (
            vc.is_playing() or vc.is_paused()
        ):
            await interaction.response.send_message(
                "❌ Es läuft gerade kein Song.",
                ephemeral=True
            )
            return

        vc.stop()

        await interaction.response.send_message(
            "⏭️ Song übersprungen!",
            ephemeral=True
        )

    @discord.ui.button(
        label="Stop",
        emoji="⏹️",
        style=discord.ButtonStyle.danger
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        music = get_music(self.guild_id)
        vc = interaction.guild.voice_client

        music.queue.clear()

        if vc is not None:

            vc.stop()

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
            ephemeral=True
        )


# ============================================================
# NÄCHSTEN SONG ABSPIELEN
# ============================================================

async def play_next(guild):

    music = get_music(guild.id)

    # Verhindert, dass mehrere play_next-Aufrufe
    # gleichzeitig denselben Song starten.
    if music.starting_song:
        return

    if music.voice_client is None:
        music.playing = False
        return

    if not music.queue:
        music.playing = False
        music.current = None

        print(
            f"Queue von {guild.name} ist leer."
        )

        return

    music.starting_song = True

    song = music.queue.popleft()
    music.current = song

    try:

        # Audio-URL erst jetzt von YouTube holen
        audio = await get_audio_url(
            song["webpage_url"]
        )

        if music.voice_client is None:
            music.playing = False
            return

        source = discord.FFmpegPCMAudio(
            audio["url"],
            **FFMPEG_OPTIONS
        )

        def after_play(error):

            if error:
                print(
                    f"Audio-Fehler: {repr(error)}"
                )

            asyncio.run_coroutine_threadsafe(
                song_finished(guild),
                bot.loop
            )

        music.voice_client.play(
            source,
            after=after_play
        )

        music.playing = True

        # History aktualisieren
        music.history.appendleft(song)

        if music.text_channel:

            embed = create_music_embed(
                song["title"],
                music.voice_client.channel.name,
                song.get("thumbnail")
            )

            try:
                await music.text_channel.send(
                    embed=embed,
                    view=MusicView(guild.id)
                )
            except Exception as error:
                print(
                    f"Fehler beim Senden des Music-Embeds: "
                    f"{repr(error)}"
                )

        print(
            f"▶️ Spiele: {song['title']}"
        )

    except asyncio.TimeoutError:

        print(
            f"⏱️ YouTube-Timeout bei: "
            f"{song['title']}"
        )

        music.playing = False
        music.current = None

        if music.text_channel:
            try:
                await music.text_channel.send(
                    f"❌ YouTube hat zu lange gebraucht für "
                    f"**{song['title']}**. Überspringe..."
                )
            except Exception:
                pass

        await play_next(guild)

    except Exception as error:

        print(
            "===================================="
        )
        print("AUDIO-FEHLER:")
        print(repr(error))
        print(
            "===================================="
        )

        music.playing = False
        music.current = None

        if music.text_channel:
            try:
                await music.text_channel.send(
                    f"❌ **{song['title']}** konnte nicht "
                    f"abgespielt werden. Überspringe..."
                )
            except Exception:
                pass

        await play_next(guild)

    finally:
        music.starting_song = False


async def song_finished(guild):

    music = get_music(guild.id)

    music.playing = False
    music.current = None

    # Kleine Pause verhindert Probleme zwischen
    # zwei FFmpeg-Prozessen.
    await asyncio.sleep(0.5)

    await play_next(guild)


# ============================================================
# /PLAY
# ============================================================

@bot.tree.command(
    name="play",
    description="Spielt einen Song von YouTube ab."
)
@app_commands.describe(
    song="Name des Songs oder YouTube-Link"
)
async def play(
    interaction: discord.Interaction,
    song: str
):

    # SOFORT Discord antworten
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

        print(
            f"🔎 YouTube-Suche: {song}"
        )

        result = await search_youtube(song)

    except asyncio.TimeoutError:

        print(
            f"⏱️ YouTube-Suche Timeout: {song}"
        )

        await interaction.followup.send(
            "❌ YouTube braucht zu lange für die Suche. "
            "Bitte versuche es nochmal."
        )
        return

    except Exception as error:

        print(
            "===================================="
        )
        print("YOUTUBE SUCHFEHLER:")
        print(repr(error))
        print(
            "===================================="
        )

        await interaction.followup.send(
            "❌ Bei der YouTube-Suche ist ein Fehler "
            "aufgetreten.\n"
            "Schau bitte in die Railway-Logs."
        )
        return

    if result is None:

        await interaction.followup.send(
            "❌ Ich konnte den Song nicht finden."
        )
        return

    song_data = {
        "title": result["title"],
        "webpage_url": result["webpage_url"],
        "thumbnail": result.get("thumbnail"),
        "duration": result.get("duration"),
    }

    # ========================================================
    # VOICE JOIN
    # ========================================================

    try:

        vc = interaction.guild.voice_client

        if vc is None:

            vc = await voice_channel.connect()

        elif vc.channel != voice_channel:

            await vc.move_to(
                voice_channel
            )

        music.voice_client = vc

    except Exception as error:

        print(
            f"Voice-Fehler: {repr(error)}"
        )

        await interaction.followup.send(
            "❌ Ich konnte dem Voice-Channel nicht beitreten."
        )
        return

    # ========================================================
    # SONG IN QUEUE
    # ========================================================

    music.queue.append(song_data)

    # ========================================================
    # STARTEN
    # ========================================================

    if not music.playing and not music.starting_song:

        await interaction.followup.send(
            f"🎵 **{song_data['title']}** wird geladen..."
        )

        await play_next(
            interaction.guild
        )

    else:

        position = len(music.queue)

        await interaction.followup.send(
            f"✅ **{song_data['title']}** wurde "
            f"zur Queue hinzugefügt.\n"
            f"📋 Position: **{position}**"
        )


# ============================================================
# /SKIP
# ============================================================

@bot.tree.command(
    name="skip",
    description="Überspringt den aktuellen Song."
)
async def skip(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    vc = interaction.guild.voice_client

    if vc is None or not (
        vc.is_playing() or vc.is_paused()
    ):

        await interaction.response.send_message(
            "❌ Es läuft gerade kein Song."
        )
        return

    vc.stop()

    await interaction.response.send_message(
        "⏭️ Song übersprungen!"
    )


# ============================================================
# /STOP
# ============================================================

@bot.tree.command(
    name="stop",
    description="Stoppt die Musik und leert die Queue."
)
async def stop(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    music = get_music(
        interaction.guild.id
    )

    music.queue.clear()

    vc = interaction.guild.voice_client

    if vc:

        vc.stop()

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

@bot.tree.command(
    name="queue",
    description="Zeigt die aktuelle Musik-Queue."
)
async def queue(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    music = get_music(
        interaction.guild.id
    )

    if not music.queue:

        await interaction.response.send_message(
            "📋 Die Queue ist leer."
        )
        return

    text = ""

    for index, song in enumerate(
        music.queue,
        start=1
    ):

        text += (
            f"**{index}.** "
            f"{song['title']}\n"
        )

    embed = discord.Embed(
        title="📋  Musik Queue",
        description=text,
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /HISTORY
# ============================================================

@bot.tree.command(
    name="history",
    description="Zeigt die letzten 10 gespielten Songs."
)
async def history(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    music = get_music(
        interaction.guild.id
    )

    if not music.history:

        await interaction.response.send_message(
            "📜 Es wurden noch keine Songs gespielt."
        )
        return

    text = ""

    for index, song in enumerate(
        music.history,
        start=1
    ):

        text += (
            f"**{index}.** "
            f"{song['title']}\n"
        )

    embed = discord.Embed(
        title="📜  Letzte 10 Songs",
        description=text,
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /PAUSE
# ============================================================

@bot.tree.command(
    name="pause",
    description="Pausiert die aktuelle Musik."
)
async def pause(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    vc = interaction.guild.voice_client

    if vc is None or not vc.is_playing():

        await interaction.response.send_message(
            "❌ Es läuft gerade kein Song."
        )
        return

    vc.pause()

    await interaction.response.send_message(
        "⏸️ Musik pausiert."
    )


# ============================================================
# /RESUME
# ============================================================

@bot.tree.command(
    name="resume",
    description="Setzt die Musik fort."
)
async def resume(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl funktioniert nur auf einem Server."
        )
        return

    vc = interaction.guild.voice_client

    if vc is None or not vc.is_paused():

        await interaction.response.send_message(
            "❌ Die Musik ist nicht pausiert."
        )
        return

    vc.resume()

    await interaction.response.send_message(
        "▶️ Musik läuft weiter."
    )


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

        print(
            f"{len(synced)} Slash Commands synchronisiert."
        )

    except Exception as error:

        print(
            f"Fehler beim Synchronisieren: "
            f"{repr(error)}"
        )


# ============================================================
# BOT START
# ============================================================

bot.run(TOKEN)
