import asyncio

import discord
import pytest

from mircewok.player import (
    GuildPlayer,
    PlayerManager,
    QueueFull,
    QueueItem,
    describe_connect_error,
)
from mircewok.sounds import Sound
from tests.fakes import (
    FakeGuild,
    FakeSource,
    FakeVoiceClient,
    fake_source_factory,
    make_channel,
    make_member,
    wait_until,
)


def sound(name="teszt"):
    from pathlib import Path

    return Sound(name, Path(f"/tmp/{name}.mp3"))


def item(channel, name="teszt", report=None):
    return QueueItem(sound=sound(name), channel=channel, requester="tesztelő", report=report)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
async def player(guild):
    p = GuildPlayer(guild, fake_source_factory, idle_timeout=5.0, max_queue=3, connect_timeout=1.0)
    yield p
    await p.shutdown()


def played_names(guild):
    vc = guild.voice_client
    return [s.sound.name for s in vc.played] if vc else []


# ----------------------------------------------------------- alap működés


async def test_plays_items_in_order_over_one_connection(guild, player):
    channel = make_channel(guild)
    for name in ("egy", "ketto", "harom"):
        player.enqueue(item(channel, name))
    await wait_until(lambda: guild.voice_client and len(guild.voice_client.played) == 3)
    assert played_names(guild) == ["egy", "ketto", "harom"]
    assert channel.connect_calls == 1


async def test_enqueue_reports_how_many_are_ahead(guild, player):
    channel = make_channel(guild, auto_finish=False)
    assert player.enqueue(item(channel, "a")) == 0
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    assert player.enqueue(item(channel, "b")) == 1
    assert player.enqueue(item(channel, "c")) == 2


async def test_queue_full_raises(guild, player):
    channel = make_channel(guild, auto_finish=False)
    player.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    for name in "bcd":  # max_queue = 3
        player.enqueue(item(channel, name))
    assert player.is_full
    with pytest.raises(QueueFull):
        player.enqueue(item(channel, "e"))


async def test_playback_runs_with_the_bots_deaf_flag(guild, player):
    channel = make_channel(guild)
    player.enqueue(item(channel))
    await wait_until(lambda: channel.connect_calls == 1)
    kwargs = channel.connect.call_args.kwargs
    assert kwargs["self_deaf"] is True and kwargs["reconnect"] is True


async def test_pending_and_busy_reflect_state(guild, player):
    channel = make_channel(guild, auto_finish=False)
    assert not player.busy
    player.enqueue(item(channel, "a"))
    assert player.busy and player.pending == 1
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    player.enqueue(item(channel, "b"))
    assert player.pending == 2
    guild.voice_client.finish()
    await wait_until(lambda: len(guild.voice_client.played) == 2)


# ---------------------------------------------------------- kihagy / stop


async def test_skip_stops_current_sound_and_moves_on(guild, player):
    channel = make_channel(guild, auto_finish=False)
    player.enqueue(item(channel, "a"))
    player.enqueue(item(channel, "b"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    assert player.skip() is True
    await wait_until(lambda: len(guild.voice_client.played) == 2)
    assert played_names(guild) == ["a", "b"]


async def test_skip_when_idle_returns_false(guild, player):
    assert player.skip() is False
    channel = make_channel(guild)
    player.enqueue(item(channel))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    await wait_until(lambda: not player.busy)
    assert player.skip() is False


async def test_stop_clears_queue_and_leaves_channel(guild, player):
    channel = make_channel(guild, auto_finish=False)
    for name in "abc":
        player.enqueue(item(channel, name))
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    vc = guild.voice_client
    dropped = await player.stop()
    assert dropped == 2
    assert guild.voice_client is None and vc.disconnects == 1 and not vc.playing
    await wait_until(lambda: not player.busy)
    assert [s.sound.name for s in vc.played] == ["a"]  # a törölt hangok nem szólnak


async def test_player_works_again_after_stop(guild, player):
    channel = make_channel(guild)
    player.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    await player.stop()
    player.enqueue(item(channel, "b"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert channel.connect_calls == 2
    assert played_names(guild) == ["b"]


async def test_stop_while_still_connecting_prevents_playback(guild, player):
    channel = make_channel(guild, connect_delay=0.15)
    channel.connect_started = asyncio.Event()
    player.enqueue(item(channel, "a"))
    await asyncio.wait_for(channel.connect_started.wait(), 1)
    await player.stop()  # megvárja a csatlakozás végét, majd bont
    await wait_until(lambda: not player.busy)
    assert guild.voice_client is None
    assert channel.connect_calls == 1
    # semmi nem szólt: a csatlakozás közben érkezett /stop elévült
    assert channel.connect.await_count == 1


async def test_stop_without_anything_is_harmless(guild, player):
    assert await player.stop() == 0


# ------------------------------------------------------------ hibakezelés


async def test_connect_failure_is_reported_and_next_item_still_plays(guild, player):
    bad = make_channel(guild, connect_error=asyncio.TimeoutError())
    good = make_channel(guild)
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    player.enqueue(item(bad, "rossz", report))
    player.enqueue(item(good, "jo"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert played_names(guild) == ["jo"]
    assert len(reports) == 1 and "Időtúllépés" in reports[0]


async def test_missing_voice_library_error_is_explained(guild, player):
    channel = make_channel(guild, connect_error=RuntimeError("davey library needed in order to use voice"))
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    player.enqueue(item(channel, "a", report))
    await wait_until(lambda: reports)
    assert "PyNaCl/davey" in reports[0]


async def test_a_crashing_report_callback_does_not_kill_the_worker(guild, player):
    bad = make_channel(guild, connect_error=RuntimeError("x"))
    good = make_channel(guild)

    async def exploding(text):
        raise ValueError("a visszajelzés is elromlott")

    player.enqueue(item(bad, "a", exploding))
    player.enqueue(item(good, "b"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert played_names(guild) == ["b"]


async def test_nobody_listening_skips_without_connecting(guild, player):
    empty = make_channel(guild, members=[make_member(bot=True)])
    listening = make_channel(guild)
    player.enqueue(item(empty, "senki"))
    player.enqueue(item(listening, "valaki"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert empty.connect_calls == 0
    assert played_names(guild) == ["valaki"]


async def test_source_factory_failure_is_reported(guild):
    def broken(_sound):
        raise OSError("nincs ffmpeg")

    p = GuildPlayer(guild, broken, idle_timeout=5.0)
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    channel = make_channel(guild)
    p.enqueue(item(channel, "a", report))
    await wait_until(lambda: reports)
    assert "ffmpeg" in reports[0]
    await p.shutdown()


async def test_play_start_failure_cleans_up_source_and_reports(guild):
    created: list[FakeSource] = []

    def factory(s):
        src = FakeSource(s)
        created.append(src)
        return src

    p = GuildPlayer(guild, factory, idle_timeout=5.0)
    channel = make_channel(guild)
    original_connect = channel.connect.side_effect

    async def connect_with_broken_play(**kwargs):
        vc = await original_connect(**kwargs)

        def broken(source, *, after=None):
            raise discord.ClientException("Already playing audio.")

        vc.play = broken
        return vc

    channel.connect.side_effect = connect_with_broken_play
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    p.enqueue(item(channel, "a", report))
    await wait_until(lambda: reports)
    assert "elindítani" in reports[0]
    assert created[0].cleaned is True  # az ffmpeg folyamat nem marad árván
    await p.shutdown()


async def test_playback_error_is_reported_and_worker_continues(guild, player):
    channel = make_channel(guild, auto_finish=False)
    reports: list[str] = []

    async def report(text):
        reports.append(text)

    player.enqueue(item(channel, "a", report))
    player.enqueue(item(channel, "b"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    guild.voice_client.finish(RuntimeError("ffmpeg elhalt"))
    await wait_until(lambda: len(guild.voice_client.played) == 2)
    assert reports and "lejátszás közben" in reports[0]


async def test_overlong_clip_is_cut_off_by_watchdog(guild):
    p = GuildPlayer(guild, fake_source_factory, idle_timeout=5.0, max_clip_seconds=0.05)
    channel = make_channel(guild, auto_finish=False)
    p.enqueue(item(channel, "vegtelen"))
    p.enqueue(item(channel, "kovetkezo"))
    await wait_until(lambda: guild.voice_client and len(guild.voice_client.played) == 2)
    assert guild.voice_client.stops >= 1
    await p.shutdown()


# --------------------------------------------------- csatornák / kapcsolatok


async def test_moves_to_the_requesters_channel_instead_of_reconnecting(guild, player):
    first = make_channel(guild)
    second = make_channel(guild)
    player.enqueue(item(first, "a"))
    player.enqueue(item(second, "b"))
    await wait_until(lambda: guild.voice_client and len(guild.voice_client.played) == 2)
    assert guild.voice_client.moves == [second]
    assert first.connect_calls == 1 and second.connect_calls == 0


async def test_stale_disconnected_client_is_replaced(guild, player):
    channel = make_channel(guild)
    stale = FakeVoiceClient(guild, channel)
    stale.connected = False
    guild.voice_client = stale
    player.enqueue(item(channel, "a"))
    await wait_until(lambda: channel.connect_calls == 1)
    assert stale.disconnects == 1
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)


async def test_idle_timeout_leaves_the_channel_and_reconnects_later(guild):
    p = GuildPlayer(guild, fake_source_factory, idle_timeout=0.05)
    channel = make_channel(guild)
    p.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    first_vc = guild.voice_client
    await wait_until(lambda: guild.voice_client is None)
    assert first_vc.disconnects == 1
    p.enqueue(item(channel, "b"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert channel.connect_calls == 2
    await p.shutdown()


async def test_item_arriving_right_after_idle_disconnect_is_not_lost(guild):
    p = GuildPlayer(guild, fake_source_factory, idle_timeout=0.03)
    channel = make_channel(guild)
    p.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client is None and channel.connect_calls == 1 and not p.busy)
    for round_ in range(5):
        p.enqueue(item(channel, f"r{round_}"))
        await wait_until(lambda: not p.busy)
        await asyncio.sleep(0.04)  # pont az üresjárati bontás környékén
    assert channel.connect_calls >= 2
    await p.shutdown()


# ----------------------------------------------------------- leállítás


async def test_shutdown_stops_worker_and_disconnects(guild):
    p = GuildPlayer(guild, fake_source_factory, idle_timeout=5.0)
    channel = make_channel(guild, auto_finish=False)
    p.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    vc = guild.voice_client
    await p.shutdown()
    assert vc.disconnects == 1 and guild.voice_client is None
    assert p._worker is None


async def test_enqueue_after_shutdown_raises(guild):
    p = GuildPlayer(guild, fake_source_factory)
    await p.shutdown()
    with pytest.raises(RuntimeError):
        p.enqueue(item(make_channel(guild)))


async def test_a_dead_worker_is_restarted_on_next_enqueue(guild, player):
    channel = make_channel(guild)
    player.enqueue(item(channel, "a"))
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    player._worker.cancel()
    await asyncio.gather(player._worker, return_exceptions=True)
    player.enqueue(item(channel, "b"))
    await wait_until(lambda: len(guild.voice_client.played) == 2)


# ----------------------------------------------------- hibaüzenetek, manager


@pytest.mark.parametrize(
    "exc,expected",
    [
        (asyncio.TimeoutError(), "Időtúllépés"),
        (RuntimeError("PyNaCl library needed in order to use voice"), "PyNaCl/davey"),
        (RuntimeError("davey library needed in order to use voice"), "PyNaCl/davey"),
        (RuntimeError("valami más"), "Nem sikerült csatlakozni"),
        (ValueError("x"), "Nem sikerült csatlakozni"),
    ],
)
def test_describe_connect_error(exc, expected):
    assert expected in describe_connect_error(exc)


def test_describe_connect_error_for_discord_errors():
    class Resp:
        status = 403
        reason = "Forbidden"

    assert "jogom" in describe_connect_error(discord.Forbidden(Resp(), "nincs jog"))


async def test_manager_creates_one_player_per_guild_and_cleans_up():
    created: list[GuildPlayer] = []

    def factory(g):
        p = GuildPlayer(g, fake_source_factory)
        created.append(p)
        return p

    manager = PlayerManager(factory)
    g1, g2 = FakeGuild(), FakeGuild()
    assert manager.get(g1) is manager.get(g1)
    assert manager.get(g1) is not manager.get(g2)
    assert manager.peek(g1.id) is created[0]
    assert manager.peek(424242) is None
    await manager.remove(g1.id)
    assert manager.peek(g1.id) is None and created[0]._closed
    await manager.shutdown_all()
    assert manager.peek(g2.id) is None and created[1]._closed
    await manager.remove(123)  # ismeretlen szerver: nem hiba
