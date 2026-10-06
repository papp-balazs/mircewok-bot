"""A bot slash parancsai: /hang, /veletlen, /hangok, /kihagy, /stop."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .player import QueueFull, QueueItem
from .sounds import MAX_NAME_LENGTH, Sound

if TYPE_CHECKING:
    from .client import MircewokBot

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 2.0
EMBED_DESCRIPTION_LIMIT = 4000  # a Discord-korlát 4096
MAX_EMBEDS_PER_MESSAGE = 10
MAX_QUERY_LENGTH = MAX_NAME_LENGTH  # ennél hosszabb hangnév nem létezhet

# A Discord markdown minden írásjel elé tett fordított perjelet escape-nek vesz.
_MARKDOWN_SPECIALS = re.compile(r"([\\*_~`|>\[\]()#-])")


# ------------------------------------------------------------------ segédek


def escape_markdown(text: str) -> str:
    """Felhasználói szöveg biztonságos visszaírása. (A discord.utils.escape_markdown
    Python 3.13-on deprecation figyelmeztetést dob, ezért saját változatot használunk.)"""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


def _code(text: str) -> str:
    """Kódként formázott név; a visszaidézőjel kicserélése megakadályozza a formázás széttörését."""
    return "`" + text.replace("`", "'") + "`"


def _cooldown_key(interaction: discord.Interaction) -> tuple[int | None, int]:
    return (interaction.guild_id, interaction.user.id)


async def _reply(interaction: discord.Interaction, text: str | None = None, **kwargs) -> None:
    """Rejtett (csak a kérőnek látszó) válasz; ha már válaszoltunk/halasztottunk, követő üzenet."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, **kwargs)


def check_channel_access(channel: discord.VoiceChannel, me: discord.Member | None) -> str | None:
    """Hibaüzenet, ha a bot nem tud a csatornában lejátszani; egyébként None."""
    if me is None:
        return None
    perms = channel.permissions_for(me)
    if not perms.view_channel or not perms.connect:
        return f"Nincs jogom csatlakozni ide: {channel.mention}."
    if not perms.speak:
        return f"Nincs jogom beszélni ebben a csatornában: {channel.mention}."
    limit = channel.user_limit
    if limit and me not in channel.members and len(channel.members) >= limit and not perms.move_members:
        return f"A(z) {channel.mention} csatorna tele van."
    return None


def chunk_names(names: list[str], limit: int = EMBED_DESCRIPTION_LIMIT) -> list[str]:
    """A neveket vesszővel elválasztott, `limit` karakternél nem hosszabb darabokra osztja."""
    chunks: list[str] = []
    current = ""
    for name in names:
        piece = _code(name)
        addition = piece if not current else ", " + piece
        if current and len(current) + len(addition) > limit:
            chunks.append(current)
            current = piece
        else:
            current += addition
    if current:
        chunks.append(current)
    return chunks


def _can_control(member: discord.Member, vc: discord.VoiceProtocol | None) -> bool:
    """Leállíthatja/kihagyhatja a lejátszást: a bot csatornájában lévő tag vagy moderátor."""
    channel = getattr(vc, "channel", None)
    if channel is None:
        return True
    voice = member.voice
    if voice is not None and voice.channel is not None and voice.channel.id == channel.id:
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.move_members


async def _queue_sound(bot: MircewokBot, interaction: discord.Interaction, sound: Sound) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _reply(interaction, "Ez a parancs csak szerveren használható.")
        return

    voice = member.voice
    channel = voice.channel if voice is not None else None
    if channel is None:
        await _reply(interaction, "Előbb lépj be egy hangcsatornába!")
        return
    if not isinstance(channel, discord.VoiceChannel):
        await _reply(interaction, "A Stage csatornák nem támogatottak, használj sima hangcsatornát.")
        return

    problem = check_channel_access(channel, guild.me)
    if problem is not None:
        await _reply(interaction, problem)
        return

    # A visszajelzés megvárja a kezdő választ, különben a követő üzenet megelőzhetné azt.
    responded = asyncio.Event()

    async def report(text: str) -> None:
        await responded.wait()
        await interaction.followup.send(text, ephemeral=True)

    item = QueueItem(sound=sound, channel=channel, requester=str(member), report=report)
    try:
        ahead = bot.players.get(guild).enqueue(item)
    except QueueFull:
        await _reply(interaction, "Tele a lejátszási sor, várj, amíg lejátszódik néhány hang.")
        return

    try:
        if ahead == 0:
            await _reply(interaction, f"▶️ **{sound.name}** – lejátszás itt: {channel.mention}")
        else:
            await _reply(interaction, f"⏳ **{sound.name}** sorba téve ({ahead} hang van előtte)")
    finally:
        responded.set()


# ------------------------------------------------------------- regisztráció


def register_commands(bot: MircewokBot) -> None:
    tree = bot.tree

    @tree.command(name="hang", description="Lejátszik egy hangot a hangcsatornádban")
    @app_commands.describe(nev="A hang neve (kezdj el gépelni, felajánlom)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS, key=_cooldown_key)
    async def hang(interaction: discord.Interaction, nev: app_commands.Range[str, 1, MAX_QUERY_LENGTH]) -> None:
        nev = nev[:MAX_QUERY_LENGTH]  # a Discord is korlátozza, ez védőháló (az üzenet legfeljebb 2000 karakter lehet)
        sound = bot.board.get(nev)
        if sound is None:
            hint = bot.board.suggest(nev)
            text = f"Nincs ilyen hang: **{escape_markdown(nev)}**."
            if hint:
                text += " Talán ezek: " + ", ".join(_code(h) for h in hint)
            text += "\nAz összes hang: `/hangok`"
            await _reply(interaction, text)
            return
        await _queue_sound(bot, interaction, sound)

    @hang.autocomplete("nev")
    async def hang_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=s.name, value=s.name) for s in bot.board.search(current)]

    @tree.command(name="veletlen", description="Egy véletlenszerű hangot játszik le")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS, key=_cooldown_key)
    async def veletlen(interaction: discord.Interaction) -> None:
        await _queue_sound(bot, interaction, bot.board.random())

    @tree.command(name="hangok", description="Kilistázza az összes elérhető hangot")
    async def hangok(interaction: discord.Interaction) -> None:
        names = bot.board.names
        chunks = chunk_names(names)
        shown = chunks[:MAX_EMBEDS_PER_MESSAGE]
        embeds = []
        for index, chunk in enumerate(shown):
            embed = discord.Embed(description=chunk, colour=discord.Colour.blurple())
            if index == 0:
                embed.title = f"Elérhető hangok ({len(names)})"
                embed.set_footer(text="Használat: /hang nev:<hang neve>")
            embeds.append(embed)
        if not embeds:
            await _reply(interaction, "Még nincs betöltött hang.")
            return
        await _reply(interaction, embeds=embeds)

    @tree.command(name="kihagy", description="Kihagyja az éppen szóló hangot")
    @app_commands.guild_only()
    async def kihagy(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "Ez a parancs csak szerveren használható.")
            return
        if not _can_control(member, guild.voice_client):
            await _reply(interaction, "Ezt csak az teheti meg, aki a bot hangcsatornájában van.")
            return
        player = bot.players.peek(guild.id)
        if player is not None and player.skip():
            await _reply(interaction, "⏭️ Kihagytam.")
        else:
            await _reply(interaction, "Most nem szól semmi.")

    @tree.command(name="stop", description="Leállítja a lejátszást, törli a sort és kilép a csatornából")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _reply(interaction, "Ez a parancs csak szerveren használható.")
            return
        player = bot.players.peek(guild.id)
        if guild.voice_client is None and (player is None or not player.busy):
            await _reply(interaction, "Most nem játszom semmit.")
            return
        if not _can_control(member, guild.voice_client):
            await _reply(interaction, "Ezt csak az teheti meg, aki a bot hangcsatornájában van.")
            return
        # A leállítás megvárhat egy folyamatban lévő csatlakozást, ez meghaladhatja a 3 másodpercet.
        await interaction.response.defer(ephemeral=True)
        dropped = await bot.players.get(guild).stop()
        text = "⏹️ Leállítva."
        if dropped:
            text += f" {dropped} sorban álló hang törölve."
        await _reply(interaction, text)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = f"Lassabban! Próbáld újra {error.retry_after:.1f} másodperc múlva."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "Ez a parancs csak szerveren használható."
        else:
            original = getattr(error, "original", error)
            command = interaction.command.qualified_name if interaction.command else "?"
            log.error("Hiba a(z) /%s parancsban", command, exc_info=original)
            text = "Váratlan hiba történt. Próbáld újra később."
        try:
            await _reply(interaction, text)
        except discord.HTTPException as exc:
            log.warning("A hibaüzenet nem küldhető el: %r", exc)
