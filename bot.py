import discord
from discord.ext import commands
from datetime import datetime
from dotenv import load_dotenv
import os
import time

# ==================================================
# .ENV LADEN
# ==================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError(
        "Kein DISCORD_TOKEN gefunden! "
        "Überprüfe deine .env Datei."
    )


# ==================================================
# EINSTELLUNGEN
# ==================================================

# ID vom Sprachkanal
VOICE_CHANNEL_ID = 1534654223923282015

# ID vom Textkanal, wo der Ping gesendet werden soll
TEXT_CHANNEL_ID = 1534656181161693334

# Cooldown: 10 Minuten
COOLDOWN = 10 * 60


# ==================================================
# BOT EINSTELLUNGEN
# ==================================================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# Speichert, wann eine Person zuletzt
# einen Ping ausgelöst hat
last_ping = {}


# ==================================================
# BOT IST ONLINE
# ==================================================

@bot.event
async def on_ready():

    print("====================================")
    print(f"Bot online: {bot.user}")
    print("Voice-Ping-System ist aktiv!")
    print("====================================")


# ==================================================
# VOICE STATE UPDATE
# ==================================================

@bot.event
async def on_voice_state_update(member, before, after):

    # Bots ignorieren
    if member.bot:
        return

    # Nur reagieren, wenn jemand einem
    # Voice-Kanal beitritt
    if after.channel is None:
        return

    # Nur unseren bestimmten Voice-Kanal überwachen
    if after.channel.id != VOICE_CHANNEL_ID:
        return

    # Wenn die Person bereits im selben Kanal war,
    # wurde z.B. nur Mikrofon geändert
    if before.channel == after.channel:
        return

    # ==================================================
    # PERSONEN IM CALL ZÄHLEN
    # ==================================================

    personen = [
        m for m in after.channel.members
        if not m.bot
    ]

    anzahl = len(personen)

    print(
        f"{member.display_name} ist "
        f"{after.channel.name} beigetreten."
    )

    print(
        f"Personen im Call: {anzahl}"
    )

    # ==================================================
    # WENN BEREITS JEMAND IM CALL IST
    # ==================================================

    # Nur wenn die Person die EINZIGE Person
    # im Sprachkanal ist, wird gepingt.

    if anzahl > 1:

        print(
            "Es ist bereits jemand im Call."
            " Kein Ping."
        )

        return

    # ==================================================
    # 10-MINUTEN-COOLDOWN PRÜFEN
    # ==================================================

    jetzt = time.time()

    if member.id in last_ping:

        vergangen = jetzt - last_ping[member.id]

        # Noch keine 10 Minuten vergangen
        if vergangen < COOLDOWN:

            verbleibend = int(
                (COOLDOWN - vergangen) / 60
            )

            print(
                f"Cooldown für "
                f"{member.display_name} aktiv."
            )

            print(
                f"Noch ungefähr "
                f"{verbleibend} Minuten."
            )

            return

    # ==================================================
    # TEXTKANAL HOLEN
    # ==================================================

    text_channel = bot.get_channel(
        TEXT_CHANNEL_ID
    )

    if text_channel is None:

        print(
            "FEHLER: Textkanal wurde nicht gefunden!"
        )

        return

    # ==================================================
    # AKTUELLE UHRZEIT
    # ==================================================

    uhrzeit = datetime.now().strftime(
        "%H:%M Uhr"
    )

    # ==================================================
    # SCHÖNE DISCORD EMBED
    # ==================================================

    embed = discord.Embed(

        title="🟢  Voice Aktiv",

        description=(
            f"🔊 **{member.display_name} ist im Call!**\n\n"
            f"👥 **Kanal:** {after.channel.name}\n"
            f"🕐 **Beigetreten:** {uhrzeit}\n\n"
            "────────────────────\n"
            "🎧 Der Voice-Chat ist jetzt aktiv!"
        ),

        # Grüner Balken links
        color=discord.Color.green()
    )

    # Footer
    embed.set_footer(
        text="Voice Notification • 10 Minuten Cooldown"
    )

    # ==================================================
    # @EVERYONE + EMBED SENDEN
    # ==================================================

    await text_channel.send(

        content="@everyone",

        embed=embed,

        allowed_mentions=discord.AllowedMentions(
            everyone=True
        )
    )

    # ==================================================
    # COOLDOWN SPEICHERN
    # ==================================================

    last_ping[member.id] = jetzt

    print(
        f"@everyone wurde gepingt, "
        f"weil {member.display_name} "
        f"den Call betreten hat."
    )


# ==================================================
# BOT STARTEN
# ==================================================

bot.run(TOKEN)
