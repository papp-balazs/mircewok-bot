import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from mircewok import app, client
from mircewok.client import INVITE_PERMISSIONS, MircewokBot
from mircewok.config import DEFAULT_SOUNDS_DIR, Config
from mircewok.sounds import SoundBoard
from tests.fakes import (
    FakeGuild,
    FakeVoiceClient,
    fake_source_factory,
    make_channel,
    make_member,
)


def make_config(**overrides):
    base = dict(token="x", ffmpeg="/bin/true", idle_timeout=5.0, max_queue=3)
    base.update(overrides)
    return Config(**base)


@pytest.fixture
async def bot():
    b = MircewokBot(make_config(), SoundBoard(DEFAULT_SOUNDS_DIR), source_factory=fake_source_factory)
    yield b
    await b.close()


# ------------------------------------------------------------- kliens


def test_only_required_non_privileged_intents_are_enabled(bot):
    assert bot.intents.value == discord.Intents(guilds=True, voice_states=True).value
    assert not bot.intents.message_content
    assert not bot.intents.members
    assert not bot.intents.presences


def test_bot_never_pings_anyone(bot):
    mentions = bot.allowed_mentions
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


def test_invite_permissions_are_minimal():
    assert INVITE_PERMISSIONS.value == 3146752  # csatornák látása + csatlakozás + beszéd
    assert client.INVITE_SCOPES == ("bot", "applications.commands")


async def test_guild_sync_is_instant_and_scoped(bot):
    bot.config = make_config(guild_id=123)
    bot.tree.sync = AsyncMock(return_value=[1, 2, 3, 4, 5])
    bot.tree.copy_global_to = MagicMock()
    await bot.sync_commands()
    bot.tree.copy_global_to.assert_called_once()
    assert bot.tree.copy_global_to.call_args.kwargs["guild"].id == 123
    assert bot.tree.sync.call_args.kwargs["guild"].id == 123


async def test_global_sync_without_guild_id(bot):
    bot.tree.sync = AsyncMock(return_value=[1, 2, 3, 4, 5])
    await bot.sync_commands()
    bot.tree.sync.assert_awaited_once_with()


async def test_sync_failure_is_logged_not_fatal(bot, caplog):
    bot.tree.sync = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="hiba"), "szerverhiba")
    )
    with caplog.at_level(logging.ERROR):
        await bot.sync_commands()  # nem dobhat kivételt
    assert "szinkronizálása nem sikerült" in caplog.text


async def test_setup_hook_syncs(bot):
    bot.sync_commands = AsyncMock()
    await bot.setup_hook()
    bot.sync_commands.assert_awaited_once()


async def test_on_ready_announces_invite_once(bot, caplog):
    bot._connection.user = SimpleNamespace(id=1234567890, __str__=lambda self: "Mircewok#0")
    with caplog.at_level(logging.INFO):
        await bot.on_ready()
        await bot.on_ready()  # újracsatlakozáskor ismét lefuthat
    assert caplog.text.count("Meghívó link") == 1
    assert "client_id=1234567890" in caplog.text
    assert "permissions=3146752" in caplog.text
    assert "applications.commands" in caplog.text


async def test_close_shuts_players_down_first(bot):
    order = []
    bot.players.shutdown_all = AsyncMock(side_effect=lambda: order.append("players"))
    original = discord.Client.close

    async def fake_close(self):
        order.append("client")

    discord.Client.close = fake_close
    try:
        await bot.close()
    finally:
        discord.Client.close = original
    assert order == ["players", "client"]


async def test_guild_removal_drops_player(bot):
    guild = FakeGuild()
    player = bot.players.get(guild)
    await bot.on_guild_remove(guild)
    assert bot.players.peek(guild.id) is None and player._closed


# ------------------------------------- „mindenki kilépett” automatika


def voice_scene(bot, *, remaining_humans=0):
    guild = FakeGuild()
    me = make_member(bot=True)
    leaver = make_member(name="kilépő")
    leaver.guild = guild
    members = [me] + [make_member() for _ in range(remaining_humans)]
    channel = make_channel(guild, members=members)
    guild.voice_client = FakeVoiceClient(guild, channel)
    player = bot.players.get(guild)
    player.stop = AsyncMock(return_value=0)
    return guild, leaver, channel, player


async def test_bot_leaves_when_last_human_leaves(bot):
    guild, leaver, channel, player = voice_scene(bot)
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    player.stop.assert_awaited_once()


async def test_bot_stays_while_humans_remain(bot):
    guild, leaver, channel, player = voice_scene(bot, remaining_humans=1)
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    player.stop.assert_not_awaited()


async def test_voice_events_that_do_not_matter_are_ignored(bot):
    guild, leaver, channel, player = voice_scene(bot)
    other = make_channel(guild)
    # némítás/hangszín változás: a csatorna ugyanaz
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=channel), SimpleNamespace(channel=channel))
    # valaki más csatornájából lépett ki
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=other), SimpleNamespace(channel=None))
    # belépés (nincs előző csatorna)
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=None), SimpleNamespace(channel=channel))
    # bot-tagok eseményei
    robot = make_member(bot=True)
    robot.guild = guild
    await bot.on_voice_state_update(robot, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    player.stop.assert_not_awaited()


async def test_voice_event_without_connection_is_ignored(bot):
    guild, leaver, channel, player = voice_scene(bot)
    guild.voice_client = None
    await bot.on_voice_state_update(leaver, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
    player.stop.assert_not_awaited()


# ---------------------------------------------------------- indítás (app)


def test_voice_prerequisites_are_present_in_this_environment():
    assert app.voice_problems() == []


def test_voice_problems_name_the_missing_pieces(monkeypatch):
    monkeypatch.setattr(app.importlib.util, "find_spec", lambda name: None)

    def no_opus():
        raise discord.opus.OpusNotLoaded()

    monkeypatch.setattr(app.discord.opus, "Encoder", no_opus)
    problems = " | ".join(app.voice_problems())
    assert "PyNaCl" in problems and "davey" in problems and "libopus" in problems


def test_main_refuses_the_placeholder_token(monkeypatch, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=<tokened-helye_a1b2c3d4e5f6g7h8i9>\n", encoding="utf-8")
    monkeypatch.setattr("mircewok.config.ENV_FILE", env)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    assert app.main() == 2
    err = capsys.readouterr().err
    assert "Konfigurációs hiba" in err and "helykitöltő" in err


def test_main_refuses_to_start_without_sounds(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(app, "load_config", lambda: make_config(sounds_dir=tmp_path))
    assert app.main() == 2
    assert "Nincs egyetlen .mp3" in capsys.readouterr().err


def test_main_refuses_to_start_when_voice_is_unusable(monkeypatch, capsys):
    monkeypatch.setattr(app, "load_config", lambda: make_config(sounds_dir=DEFAULT_SOUNDS_DIR))
    monkeypatch.setattr(app, "voice_problems", lambda: ["hiányzik a davey csomag"])
    assert app.main() == 2
    assert "davey" in capsys.readouterr().err


def test_main_reports_a_rejected_token_clearly(monkeypatch, capsys):
    monkeypatch.setattr(app, "load_config", lambda: make_config(sounds_dir=DEFAULT_SOUNDS_DIR))

    def rejected(self, token, **kwargs):
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(MircewokBot, "run", rejected)
    assert app.main() == 1
    err = capsys.readouterr().err
    assert "elutasította a tokent" in err and "Reset Token" in err


def test_main_runs_the_bot_with_logging_for_all_modules(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        app, "load_config", lambda: make_config(sounds_dir=DEFAULT_SOUNDS_DIR, log_level=logging.DEBUG)
    )

    def fake_run(self, token, **kwargs):
        seen.update(token=token, **kwargs)

    monkeypatch.setattr(MircewokBot, "run", fake_run)
    assert app.main() == 0
    assert seen == {"token": "x", "log_level": logging.DEBUG, "root_logger": True}


@pytest.mark.parametrize(
    "error",
    [
        discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Host not in allowlist"),
        discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Discord leállt"),
        aiohttp.ClientError("Cannot connect to host discord.com"),
        OSError("Network is unreachable"),
    ],
    ids=["forbidden", "http-503", "aiohttp", "oserror"],
)
def test_main_turns_network_failures_into_a_clear_message(monkeypatch, capsys, error):
    monkeypatch.setattr(app, "load_config", lambda: make_config(sounds_dir=DEFAULT_SOUNDS_DIR))

    def unreachable(self, token, **kwargs):
        raise error

    monkeypatch.setattr(MircewokBot, "run", unreachable)
    assert app.main() == 1
    err = capsys.readouterr().err
    assert "Nem sikerült kapcsolódni a Discordhoz" in err and "Traceback" not in err
