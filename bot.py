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
import wavelink

# ============================================================
# .ENV / RAILWAY
# ============================================================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN wurde nicht gefunden. Lege die Variable in Railway an.")

# Verbindung zum separat laufenden Lavalink-Server (eigener Service/Container).
# LAVALINK_URI z.B. "http://lavalink.railway.internal:2333"
LAVALINK_URI = os.getenv("LAVALINK_URI", "http://127.0.0.1:2333")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

# ============================================================
# EINSTELLUNGEN
# ============================================================
VOICE_CHANNEL_ID = 1534654223923282015
TEXT_CHANNEL_ID = 1534656181161693334
VOICE_COOLDOWN = 10 * 60
MAX_HISTORY = 10
AUTO_DISCONNECT_DELAY = 15 * 60  # 15 Minuten

# ============================================================
# DISCORD INTENTS
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
# VOICE-PING SYSTEM (unveraendert)
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
            print(f"Cooldown aktiv für {member.display_name}. Noch ca. {verbleibend} Minuten.")
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
    """Haelt nur noch UI-/Zusatzstate. Die eigentliche Queue/Wiedergabe
    laeuft ueber wavelink.Player (player.queue, player.playing)."""

    def __init__(self):
        self.history = deque(maxlen=MAX_HISTORY)
        self.text_channel = None
        self.liked = set()
        self.now_playing_message = None
        self.update_task = None
        self.disconnect_task = None


guild_music = {}


def get_music(guild_id):
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()
    return guild_music[guild_id]


# ============================================================
# LAVALINK NODE VERBINDEN
# ============================================================
async def connect_lavalink():
    node = wavelink.Node(uri=LAVALINK_URI, password=LAVALINK_PASSWORD)
    await wavelink.Pool.connect(nodes=[node], client=bot)


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"Lavalink-Node verbunden: {payload.node.identifier} (resumed={payload.resumed})")


# ============================================================
# HILFSFUNKTIONEN
# ============================================================
def format_duration(ms):
    if ms is None:
        return "--:--"
    seconds = max(0, int(ms / 1000))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def build_progress_bar(elapsed_ms, duration_ms, length=18):
    if not duration_ms:
        return "▬" * length
    frac = max(0.0, min(1.0, elapsed_ms / duration_ms))
    pos = int(frac * (length - 1))
    return "▬" * pos + "🔘" + "▬" * (length - 1 - pos)


def create_music_embed(player: wavelink.Player, music: GuildMusic, channel_name):
    track = player.current
    elapsed_ms = player.position
    duration_ms = track.length
    bar = build_progress_bar(elapsed_ms, duration_ms)
    time_text = f"{format_duration(elapsed_ms)} / {format_duration(duration_ms)}"
    liked = track.title in music.liked

    embed = discord.Embed(
        title="🎵  Now Playing",
        description=(
            f"**{track.title}**\n\n"
            f"{bar}\n"
            f"`{time_text}`\n\n"
            f"🔊 **Voice:** {channel_name}\n"
            f"🔉 **Lautstärke:** {player.volume}%   •   "
            f"🔁 **Wiederholen:** {'An' if player.queue.mode == wavelink.QueueMode.loop else 'Aus'}   •   "
            f"{'❤️' if liked else '🤍'} **Geliked:** {'Ja' if liked else 'Nein'}"
        ),
        color=discord.Color.blurple(),
    )
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    embed.set_footer(text="Music Bot • Lavalink")
    return embed


async def now_playing_updater(guild_id):
    music = get_music(guild_id)
    player: wavelink.Player = bot.get_guild(guild_id).voice_client

    while (
        player is not None
        and player.connected
        and player.current is not None
        and music.now_playing_message is not None
    ):
        try:
            embed = create_music_embed(player, music, player.channel.name)
            await music.now_playing_message.edit(embed=embed)
        except discord.HTTPException:
            pass
        except Exception as error:
            print(f"Fehler beim Aktualisieren des Now-Playing-Widgets: {repr(error)}")
            break
        await asyncio.sleep(5)


async def send_now_playing(player: wavelink.Player, music: GuildMusic):
    if music.text_channel is None:
        return
    embed = create_music_embed(player, music, player.channel.name)
    music.now_playing_message = await music.text_channel.send(
        embed=embed, view=MusicView(player.guild.id)
    )
    if music.update_task and not music.update_task.done():
        music.update_task.cancel()
    music.update_task = asyncio.create_task(now_playing_updater(player.guild.id))


# ============================================================
# AUTO-DISCONNECT NACH 15 MINUTEN OHNE MUSIK
# ============================================================
def cancel_disconnect_timer(music):
    if music.disconnect_task and not music.disconnect_task.done():
        music.disconnect_task.cancel()
    music.disconnect_task = None


def start_disconnect_timer(guild):
    music = get_music(guild.id)
    cancel_disconnect_timer(music)
    music.disconnect_task = asyncio.create_task(auto_disconnect_after_idle(guild))


async def auto_disconnect_after_idle(guild):
    music = get_music(guild.id)
    try:
        print(f"[{guild.name}] Keine Musik mehr -> Auto-Disconnect in 15 Minuten.")
        await asyncio.sleep(AUTO_DISCONNECT_DELAY)

        player: wavelink.Player = guild.voice_client
        if player is not None and (player.playing or not player.queue.is_empty):
            print(f"[{guild.name}] Wieder Musik vorhanden -> Auto-Disconnect abgebrochen.")
            return

        if player is not None and player.connected:
            print(f"[{guild.name}] 15 Minuten ohne Musik -> Bot verlässt den Voice-Channel.")
            await player.disconnect()

        music.now_playing_message = None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[{guild.name}] Fehler beim Auto-Disconnect: {type(error).__name__}: {error}")
    finally:
        music.disconnect_task = None


# ============================================================
# TRACK-ENDE -> NAECHSTER SONG (wavelink macht das Laden/Streamen selbst,
# hier nur noch: Verlauf pflegen, Embed aktualisieren, Auto-Disconnect)
# ============================================================
@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if player is None:
        return

    guild = player.guild
    music = get_music(guild.id)

    if payload.track:
        music.history.appendleft(payload.track)

    if music.update_task and not music.update_task.done():
        music.update_task.cancel()

    # wavelink spielt bei aktiver Queue den naechsten Track automatisch
    # (autoplay=partial). Wir muessen nur noch das Embed nachziehen bzw.
    # den Idle-Timer starten, wenn nichts mehr kommt.
    await asyncio.sleep(0.3)  # kurzer Moment, damit player.current gesetzt ist

    if player.current is not None:
        await send_now_playing(player, music)
    else:
        print(f"Queue von {guild.name} ist leer.")
        start_disconnect_timer(guild)


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    if player is None:
        return
    guild = player.guild
    music = get_music(guild.id)
    cancel_disconnect_timer(music)
    print(f"Spiele: {payload.track.title}")


# ============================================================
# MUSIC BUTTONS
# ============================================================
class MusicView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def _player(self, interaction) -> wavelink.Player | None:
        return interaction.guild.voice_client

    @discord.ui.button(label="Zurück", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        player = self._player(interaction)

        if player is None or len(music.history) < 2:
            await interaction.response.send_message("❌ Es gibt keinen vorherigen Song.", ephemeral=True)
            return

        previous_track = music.history[1]
        player.queue.put_at(0, previous_track)
        await player.skip()
        await interaction.response.send_message(f"⏮️ Spiele erneut: **{previous_track.title}**", ephemeral=True)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction)
        if player is None:
            await interaction.response.send_message("❌ Der Bot ist nicht im Voice-Channel.", ephemeral=True)
            return

        await player.pause(not player.paused)
        button.label = "Fortsetzen" if player.paused else "Pause"
        button.emoji = "▶️" if player.paused else "⏸️"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message("❌ Es läuft gerade kein Song.", ephemeral=True)
            return
        await player.skip()
        await interaction.response.send_message("⏭️ Song übersprungen!", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        player = self._player(interaction)
        cancel_disconnect_timer(music)

        if music.update_task and not music.update_task.done():
            music.update_task.cancel()

        if player is not None:
            player.queue.clear()
            await player.stop()
            await player.disconnect()

        music.now_playing_message = None
        await interaction.response.send_message("⏹️ Musik gestoppt und Queue geleert.", ephemeral=True)

    @discord.ui.button(label="Like", emoji="🤍", style=discord.ButtonStyle.secondary, row=0)
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        music = get_music(self.guild_id)
        player = self._player(interaction)

        if player is None or player.current is None:
            await interaction.response.send_message("❌ Aktuell läuft kein Song.", ephemeral=True)
            return

        title = player.current.title
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

    @discord.ui.button(label="Leiser", emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def volume_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction)
        if player is None:
            return
        await player.set_volume(max(0, player.volume - 10))
        await interaction.response.send_message(f"🔉 Lautstärke: **{player.volume}%**", ephemeral=True, delete_after=3)

    @discord.ui.button(label="Lauter", emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def volume_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction)
        if player is None:
            return
        await player.set_volume(min(200, player.volume + 10))
        await interaction.response.send_message(f"🔊 Lautstärke: **{player.volume}%**", ephemeral=True, delete_after=3)

    @discord.ui.button(label="Repeat: Aus", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction)
        if player is None:
            return
        repeat_on = player.queue.mode != wavelink.QueueMode.loop
        player.queue.mode = wavelink.QueueMode.loop if repeat_on else wavelink.QueueMode.normal
        button.label = f"Repeat: {'An' if repeat_on else 'Aus'}"
        button.style = discord.ButtonStyle.success if repeat_on else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)


# ============================================================
# /PLAY
# ============================================================
@bot.tree.command(name="play", description="Spielt einen Song ab (YouTube-Link oder Suchbegriff).")
@app_commands.describe(song="Name des Songs oder YouTube-Link")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer()

    if interaction.guild is None:
        await interaction.followup.send("❌ Dieser Befehl funktioniert nur auf einem Server.")
        return
    if interaction.user.voice is None:
        await interaction.followup.send("❌ Du musst zuerst in einem Voice-Channel sein.")
        return

    voice_channel = interaction.user.voice.channel
    music = get_music(interaction.guild.id)
    music.text_channel = interaction.channel
    cancel_disconnect_timer(music)

    # Suche/Auflösung läuft ueber den Lavalink-Node (kein eigener yt-dlp-
    # Prozess mehr im Bot). wavelink haengt bei reinem Suchtext selbst
    # schon ein Praefix an (z.B. ytmsearch:) - hier NICHT nochmal eins
    # voranstellen, sonst entsteht ein ungueltiges Doppel-Praefix.
    try:
        results = await wavelink.Playable.search(song)
    except Exception as error:
        print(f"Fehler bei Lavalink-Suche: {type(error).__name__}: {error}")
        await interaction.followup.send(f"❌ Fehler bei der Suche:\n```{str(error)[:1800]}```")
        return

    if not results:
        await interaction.followup.send("❌ Ich konnte den Song nicht finden.")
        return

    track = results[0] if not isinstance(results, wavelink.Playlist) else results.tracks[0]

    player: wavelink.Player = interaction.guild.voice_client
    if player is None:
        try:
            player = await voice_channel.connect(cls=wavelink.Player)
        except Exception as error:
            print(f"Fehler beim Voice-Connect: {type(error).__name__}: {error}")
            await interaction.followup.send(
                f"❌ Ich konnte dem Voice-Channel nicht beitreten.\n"
                f"Fehler: `{type(error).__name__}: {str(error)[:300]}`"
            )
            return
        player.autoplay = wavelink.AutoPlayMode.partial
    elif player.channel != voice_channel:
        await player.move_to(voice_channel)

    if player.playing:
        await player.queue.put_wait(track)
        await interaction.followup.send(
            f"✅ **{track.title}** wurde zur Queue hinzugefügt.\n"
            f"📋 Position: **{len(player.queue)}**"
        )
    else:
        await player.play(track)
        await interaction.followup.send(f"🎵 **{track.title}** wird abgespielt.")


# ============================================================
# /SKIP /STOP /QUEUE /HISTORY /PAUSE /RESUME
# ============================================================
@bot.tree.command(name="skip", description="Überspringt den aktuellen Song.")
async def skip(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client if interaction.guild else None
    if player is None or player.current is None:
        await interaction.response.send_message("❌ Es läuft gerade kein Song.")
        return
    await player.skip()
    await interaction.response.send_message("⏭️ Song übersprungen!")


@bot.tree.command(name="stop", description="Stoppt die Musik und leert die Queue.")
async def stop(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    music = get_music(interaction.guild.id)
    cancel_disconnect_timer(music)
    if music.update_task and not music.update_task.done():
        music.update_task.cancel()

    player: wavelink.Player = interaction.guild.voice_client
    if player is not None:
        player.queue.clear()
        await player.stop()
        await player.disconnect()

    music.now_playing_message = None
    await interaction.response.send_message("⏹️ Musik gestoppt und Queue geleert.")


@bot.tree.command(name="queue", description="Zeigt die aktuelle Musik-Queue.")
async def queue(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client if interaction.guild else None
    if player is None or player.queue.is_empty:
        await interaction.response.send_message("📋 Die Queue ist leer.")
        return

    text = "\n".join(f"**{i}.** {t.title}" for i, t in enumerate(player.queue, start=1))
    embed = discord.Embed(title="📋  Musik Queue", description=text, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="history", description="Zeigt die letzten 10 gespielten Songs.")
async def history(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server möglich.")
        return

    music = get_music(interaction.guild.id)
    if not music.history:
        await interaction.response.send_message("📜 Es wurden noch keine Songs gespielt.")
        return

    text = "\n".join(f"**{i}.** {t.title}" for i, t in enumerate(music.history, start=1))
    embed = discord.Embed(title="📜  Letzte 10 Songs", description=text, color=discord.Color.green())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="pause", description="Pausiert die aktuelle Musik.")
async def pause(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client if interaction.guild else None
    if player is None or not player.playing or player.paused:
        await interaction.response.send_message("❌ Es läuft gerade kein Song.")
        return
    await player.pause(True)
    await interaction.response.send_message("⏸️ Musik pausiert.")


@bot.tree.command(name="resume", description="Setzt die Musik fort.")
async def resume(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client if interaction.guild else None
    if player is None or not player.paused:
        await interaction.response.send_message("❌ Die Musik ist nicht pausiert.")
        return
    await player.pause(False)
    await interaction.response.send_message("▶️ Musik läuft weiter.")


# ============================================================
# BOT READY
# ============================================================
@bot.event
async def on_ready():
    print("====================================")
    print(f"Bot online: {bot.user}")
    print("Voice-Ping-System: AKTIV")
    print("Music-System: AKTIV (Lavalink)")
    print(f"Auto-Disconnect: {AUTO_DISCONNECT_DELAY // 60} Minuten")
    print(f"Lavalink-URI: {LAVALINK_URI}")
    print("====================================")

    try:
        await connect_lavalink()
    except Exception as error:
        print(f"FEHLER: Konnte nicht zum Lavalink-Node verbinden: {type(error).__name__}: {error}")

    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} Slash Commands synchronisiert.")
    except Exception as error:
        print(f"Fehler beim Synchronisieren: {type(error).__name__}: {error}")


# ============================================================
# BOT START
# ============================================================
bot.run(TOKEN)
