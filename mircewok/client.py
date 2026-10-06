"""A Discord kliens: eseménykezelők, parancsszinkron, szerverenkénti lejátszók."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .commands import register_commands
from .config import Config
from .player import GuildPlayer, PlayerManager, SourceFactory, ffmpeg_source_factory
from .sounds import SoundBoard

log = logging.getLogger(__name__)

# Egy hangbotnak ennyi jog kell: csatornák látása, csatlakozás, beszéd.
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, connect=True, speak=True)
INVITE_SCOPES = ("bot", "applications.commands")


class MircewokBot(discord.Client):
    def __init__(
        self,
        config: Config,
        board: SoundBoard,
        *,
        source_factory: SourceFactory | None = None,
    ) -> None:
        # Privilegizált intent (üzenet-tartalom, tagok, jelenlét) NEM kell: slash parancsokat
        # használunk, a hanghoz pedig a guilds + voice_states elég.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())

        self.config = config
        self.board = board
        self.tree = app_commands.CommandTree(self)
        factory = source_factory or ffmpeg_source_factory(config.ffmpeg)
        self.players = PlayerManager(
            lambda guild: GuildPlayer(
                guild,
                factory,
                idle_timeout=config.idle_timeout,
                max_queue=config.max_queue,
            )
        )
        self._announced = False
        register_commands(self)

    async def setup_hook(self) -> None:
        await self.sync_commands()

    async def sync_commands(self) -> None:
        """Feltölti a slash parancsokat a Discordra.

        GUILD_ID megadása esetén egyetlen szerverre szinkronizál (azonnal látszik),
        egyébként globálisan (ez a Discordnál késhet néhány percet).
        """
        try:
            if self.config.guild_id is not None:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                where = f"a(z) {self.config.guild_id} szerveren"
            else:
                synced = await self.tree.sync()
                where = "globálisan"
        except discord.HTTPException:
            log.exception("A parancsok szinkronizálása nem sikerült; a régi parancskészlet marad érvényben.")
            return
        log.info("%d parancs szinkronizálva %s.", len(synced), where)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Bejelentkezve mint %s (%d szerver)", self.user, len(self.guilds))
        if self._announced:  # az on_ready újracsatlakozáskor is lefuthat
            return
        self._announced = True
        invite = discord.utils.oauth_url(
            self.user.id, permissions=INVITE_PERMISSIONS, scopes=INVITE_SCOPES
        )
        log.info("Meghívó link a szerverekhez: %s", invite)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Ha a bot mellől mindenki kilépett, leáll a lejátszás és a bot is kilép."""
        if member.bot or before.channel is None or before.channel == after.channel:
            return
        vc = member.guild.voice_client
        if vc is None or getattr(vc, "channel", None) != before.channel:
            return
        if any(not m.bot for m in before.channel.members):
            return
        player = self.players.peek(member.guild.id)
        if player is not None:
            await player.stop()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.players.remove(guild.id)

    async def close(self) -> None:
        await self.players.shutdown_all()
        await super().close()
