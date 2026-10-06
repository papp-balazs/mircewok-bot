"""Hamis Discord-objektumok a tesztekhez (élő Discord nélkül)."""

from __future__ import annotations

import asyncio
import threading
from itertools import count
from unittest.mock import AsyncMock, MagicMock

import discord

from mircewok.sounds import Sound

_ids = count(1000)


class FakeSource:
    def __init__(self, sound: Sound) -> None:
        self.sound = sound
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


def fake_source_factory(sound: Sound) -> FakeSource:
    return FakeSource(sound)


class FakeGuild:
    def __init__(self) -> None:
        self.id = next(_ids)
        self.voice_client: FakeVoiceClient | None = None
        self.me = None


class FakeVoiceClient:
    """A discord.VoiceClient lejátszáshoz használt részének utánzata."""

    def __init__(self, guild: FakeGuild, channel, *, auto_finish: bool = True) -> None:
        self.guild = guild
        self.channel = channel
        self.connected = True
        self.playing = False
        self.auto_finish = auto_finish
        self.played: list[FakeSource] = []
        self.moves: list = []
        self.disconnects = 0
        self.stops = 0
        self._after = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def play(self, source, *, after=None) -> None:
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        if self.playing:
            raise discord.ClientException("Already playing audio.")
        self.playing = True
        self.played.append(source)
        self._after = after
        if self.auto_finish:
            # A valódi lejátszó külön szálon hívja az after-t, ezt is úgy utánozzuk.
            threading.Timer(0.01, self.finish).start()

    def finish(self, error: Exception | None = None) -> None:
        if not self.playing:
            return
        self.playing = False
        after, self._after = self._after, None
        if after is not None:
            after(error)

    def stop(self) -> None:
        self.stops += 1
        self.finish()

    async def move_to(self, channel) -> None:
        self.channel = channel
        self.moves.append(channel)

    async def disconnect(self, *, force: bool = False) -> None:
        self.connected = False
        self.disconnects += 1
        if self.guild.voice_client is self:
            self.guild.voice_client = None


def make_member(*, bot: bool = False, name: str = "tag") -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.bot = bot
    member.id = next(_ids)
    member.__str__ = lambda self: name  # type: ignore[assignment]
    member.voice = None
    member.guild_permissions = discord.Permissions.none()
    return member


def make_channel(
    guild: FakeGuild,
    *,
    members: list | None = None,
    auto_finish: bool = True,
    connect_error: BaseException | None = None,
    connect_delay: float = 0.0,
    user_limit: int = 0,
    perms: discord.Permissions | None = None,
) -> MagicMock:
    """Hamis hangcsatorna; a `connect()` FakeVoiceClient-et hoz létre."""
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = next(_ids)
    channel.guild = guild
    channel.members = members if members is not None else [make_member()]
    channel.mention = f"<#{channel.id}>"
    channel.user_limit = user_limit
    channel.permissions_for = MagicMock(
        return_value=perms or discord.Permissions(view_channel=True, connect=True, speak=True)
    )
    channel.connect_calls = 0
    channel.connect_started = None  # a tesztek asyncio.Event-et tehetnek ide

    async def connect(*, timeout: float = 30.0, reconnect: bool = True, self_deaf: bool = False):
        channel.connect_calls += 1
        if channel.connect_started is not None:
            channel.connect_started.set()
        if connect_delay:
            await asyncio.sleep(connect_delay)
        if connect_error is not None:
            raise connect_error
        vc = FakeVoiceClient(guild, channel, auto_finish=auto_finish)
        guild.voice_client = vc
        return vc

    channel.connect = AsyncMock(side_effect=connect)
    return channel


async def wait_until(predicate, timeout: float = 2.0) -> None:
    """Addig vár, amíg a feltétel igaz nem lesz; különben AssertionError."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("A feltétel nem teljesült időben")
        await asyncio.sleep(0.005)


class FakeResponse:
    def __init__(self, log: list) -> None:
        self.done = False
        self.log = log
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kwargs) -> None:
        self.done = True
        self.messages.append({"content": content, **kwargs})
        self.log.append(("response", content))

    async def defer(self, **kwargs) -> None:
        self.done = True
        self.log.append(("defer", kwargs))


class FakeFollowup:
    def __init__(self, log: list) -> None:
        self.log = log
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})
        self.log.append(("followup", content))


def make_interaction(member, guild, *, command=None) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.guild = guild
    interaction.guild_id = getattr(guild, "id", None)
    interaction.command = command
    interaction.log = []
    interaction.response = FakeResponse(interaction.log)
    interaction.followup = FakeFollowup(interaction.log)
    return interaction
