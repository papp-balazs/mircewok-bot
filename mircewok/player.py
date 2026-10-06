"""Szerverenkénti lejátszási sor és hangcsatorna-kezelés.

Minden szerverhez (guild) egyetlen `GuildPlayer` tartozik, benne egy háttérmunkás
(worker) feladattal. A worker az EGYETLEN, amely hangkapcsolatot nyit, vált vagy
üresjárat miatt bont; a `/stop` és a leállítás a `_voice_lock`-on keresztül
szinkronizál vele, így nincs versenyhelyzet csatlakozás és lejátszás között.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from .sounds import Sound

log = logging.getLogger(__name__)

# Biztonsági korlát: ennél hosszabb ideig egy hang sem szólhat (beragadt lejátszás ellen).
MAX_CLIP_SECONDS = 300.0

Report = Callable[[str], Awaitable[None]]
SourceFactory = Callable[[Sound], discord.AudioSource]


class QueueFull(Exception):
    """A szerver lejátszási sora megtelt."""


@dataclass(slots=True)
class QueueItem:
    sound: Sound
    channel: discord.VoiceChannel
    requester: str = "?"
    # Aszinkron visszajelzés a kérőnek, ha a hang később mégsem játszható le.
    report: Report | None = None
    epoch: int = 0  # az enqueue() állítja be; a /stop után elévül


def ffmpeg_source_factory(ffmpeg: str) -> SourceFactory:
    """Hangforrás-gyár helyi mp3 fájlokhoz."""

    def make(sound: Sound) -> discord.AudioSource:
        # Az útvonal abszolút (lásd SoundBoard), így az ffmpeg nem értelmezheti
        # kapcsolónak vagy protokollnak.
        return discord.FFmpegPCMAudio(str(sound.path), executable=ffmpeg, options="-vn")

    return make


def describe_connect_error(exc: BaseException) -> str:
    """Felhasználóbarát üzenet egy hangcsatlakozási hibához."""
    if isinstance(exc, asyncio.TimeoutError):
        return "Időtúllépés: nem sikerült csatlakozni a hangcsatornához. Próbáld újra."
    if isinstance(exc, discord.Forbidden):
        return "Nincs jogom csatlakozni ehhez a hangcsatornához."
    if isinstance(exc, discord.ConnectionClosed):
        return f"A Discord lezárta a hangkapcsolatot (kód: {exc.code}). Próbáld újra."
    if isinstance(exc, RuntimeError) and ("PyNaCl" in str(exc) or "davey" in str(exc)):
        return "A bot hangkönyvtárai hiányoznak (PyNaCl/davey). Szólj a bot üzemeltetőjének."
    return "Nem sikerült csatlakozni a hangcsatornához."


def _has_listeners(channel: discord.VoiceChannel) -> bool:
    return any(not member.bot for member in channel.members)


class GuildPlayer:
    def __init__(
        self,
        guild: discord.Guild,
        source_factory: SourceFactory,
        *,
        idle_timeout: float = 60.0,
        max_queue: int = 10,
        connect_timeout: float = 30.0,
        max_clip_seconds: float = MAX_CLIP_SECONDS,
    ) -> None:
        self.guild = guild
        self._make_source = source_factory
        self._idle_timeout = idle_timeout
        self._connect_timeout = connect_timeout
        self._max_clip_seconds = max_clip_seconds
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=max_queue)
        self._voice_lock = asyncio.Lock()
        self._current: QueueItem | None = None
        self._epoch = 0
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------ állapot

    @property
    def pending(self) -> int:
        """Hány hang van folyamatban vagy sorban (a lejátszás alatt lévővel együtt)."""
        return self._queue.qsize() + (1 if self._current is not None else 0)

    @property
    def busy(self) -> bool:
        return self.pending > 0

    @property
    def is_full(self) -> bool:
        return self._queue.full()

    # --------------------------------------------------------------- műveletek

    def enqueue(self, item: QueueItem) -> int:
        """Sorba teszi a hangot. Visszaadja, hány hang van előtte (0 = azonnal szól)."""
        if self._closed:
            raise RuntimeError("A lejátszó már le van állítva.")
        ahead = self.pending
        item.epoch = self._epoch
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            raise QueueFull from None
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(
                self._run(), name=f"mircewok-player-{self.guild.id}"
            )
        return ahead

    def skip(self) -> bool:
        """Megszakítja az éppen szóló hangot. Igaz, ha volt mit kihagyni."""
        vc = self.guild.voice_client
        if vc is not None and vc.is_playing():
            vc.stop()
            return True
        return False

    async def stop(self) -> int:
        """Leállít mindent, kiüríti a sort és kilép a csatornából.

        Visszaadja, hány sorban álló hangot törölt.
        """
        self._epoch += 1  # a már kivett, de még el nem kezdett hang is elévül
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        vc = self.guild.voice_client
        if vc is not None and vc.is_playing():
            vc.stop()
        await self._disconnect()
        return dropped

    async def shutdown(self) -> None:
        """Végleges leállítás (a bot kilépésekor vagy a szerver elhagyásakor)."""
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await self._disconnect()

    # ------------------------------------------------------------------ belső

    async def _run(self) -> None:
        while True:
            vc = self.guild.voice_client
            connected = vc is not None and vc.is_connected()
            try:
                if connected:
                    # Üresjárat: ha egy ideig nem jön új kérés, kilépünk a csatornából.
                    item = await asyncio.wait_for(self._queue.get(), timeout=self._idle_timeout)
                else:
                    item = await self._queue.get()
            except asyncio.TimeoutError:
                await self._disconnect()
                continue

            self._current = item
            try:
                await self._play(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Váratlan hiba a(z) %s hang lejátszásakor", item.sound.name)
            finally:
                self._current = None

    async def _play(self, item: QueueItem) -> None:
        if item.epoch != self._epoch:
            log.debug("Elévült kérés kihagyva: %s", item.sound.name)
            return
        if not _has_listeners(item.channel):
            log.info("Senki sincs a(z) %s csatornában, kihagyom: %s", item.channel, item.sound.name)
            return

        try:
            async with self._voice_lock:
                if item.epoch != self._epoch:
                    return
                vc = await self._ensure_connected(item.channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Nem sikerült csatlakozni (%s): %r", item.channel, exc)
            await self._report(item, describe_connect_error(exc))
            return

        if item.epoch != self._epoch:
            return  # közben /stop érkezett; az már gondoskodik a kilépésről

        try:
            source = self._make_source(item.sound)
        except Exception as exc:
            log.error("Nem hozható létre hangforrás (%s): %r", item.sound.path, exc)
            await self._report(item, "A hang nem játszható le (ffmpeg hiba). Szólj a bot üzemeltetőjének.")
            return

        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        errors: list[BaseException] = []

        def after(error: Exception | None) -> None:
            # A lejátszó szálából hívódik.
            if error is not None:
                errors.append(error)
            try:
                loop.call_soon_threadsafe(done.set)
            except RuntimeError:  # az eseményhurok már leállt
                pass

        try:
            vc.play(source, after=after)
        except Exception as exc:
            source.cleanup()
            log.error("A lejátszás nem indítható (%s): %r", item.sound.name, exc)
            await self._report(item, "Nem sikerült elindítani a lejátszást.")
            return

        log.info("Lejátszás: %s (kérte: %s, csatorna: %s)", item.sound.name, item.requester, item.channel)
        try:
            await asyncio.wait_for(done.wait(), timeout=self._max_clip_seconds)
        except asyncio.TimeoutError:
            log.warning("A(z) %s hang túllépte a %.0f másodperces korlátot, leállítom.", item.sound.name, self._max_clip_seconds)
            vc.stop()
        except asyncio.CancelledError:
            vc.stop()
            raise

        if errors:
            log.error("Lejátszási hiba (%s): %r", item.sound.name, errors[0])
            await self._report(item, "Hiba történt a lejátszás közben.")

    async def _ensure_connected(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        """Csatlakozik a csatornához, vagy átlép oda, ha máshol van. A `_voice_lock` alatt hívandó."""
        vc = self.guild.voice_client
        if vc is not None and vc.is_connected():
            if vc.channel is None or vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc  # type: ignore[return-value]
        if vc is not None:
            # Megmaradt, már nem működő kapcsolat: töröljük, mielőtt újat nyitunk.
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                log.debug("A régi hangkapcsolat bontása nem sikerült: %r", exc)
        return await channel.connect(timeout=self._connect_timeout, reconnect=True, self_deaf=True)

    async def _disconnect(self) -> None:
        async with self._voice_lock:
            vc = self.guild.voice_client
            if vc is not None:
                try:
                    await vc.disconnect(force=True)
                except Exception as exc:
                    log.warning("A hangkapcsolat bontása nem sikerült: %r", exc)

    @staticmethod
    async def _report(item: QueueItem, text: str) -> None:
        if item.report is None:
            return
        try:
            await item.report(text)
        except Exception as exc:
            log.debug("A visszajelzés nem küldhető el: %r", exc)


class PlayerManager:
    """Szerverenként egy `GuildPlayer`, igény szerint létrehozva."""

    def __init__(self, factory: Callable[[discord.Guild], GuildPlayer]) -> None:
        self._factory = factory
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = self._players[guild.id] = self._factory(guild)
        return player

    def peek(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    async def remove(self, guild_id: int) -> None:
        player = self._players.pop(guild_id, None)
        if player is not None:
            await player.shutdown()

    async def shutdown_all(self) -> None:
        players, self._players = list(self._players.values()), {}
        await asyncio.gather(*(p.shutdown() for p in players), return_exceptions=True)
