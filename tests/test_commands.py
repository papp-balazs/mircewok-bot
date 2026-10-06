import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from mircewok.client import MircewokBot
from mircewok.commands import (
    EMBED_DESCRIPTION_LIMIT,
    check_channel_access,
    chunk_names,
    escape_markdown,
)
from mircewok.config import DEFAULT_SOUNDS_DIR, Config
from mircewok.sounds import SoundBoard
from tests.fakes import (
    FakeGuild,
    fake_source_factory,
    make_channel,
    make_interaction,
    make_member,
    wait_until,
)


@pytest.fixture
async def bot():
    config = Config(token="x", ffmpeg="/bin/true", idle_timeout=5.0, max_queue=3)
    b = MircewokBot(config, SoundBoard(DEFAULT_SOUNDS_DIR), source_factory=fake_source_factory)
    yield b
    await b.close()


def scenario(*, in_voice=True, auto_finish=True, **channel_kwargs):
    guild = FakeGuild()
    guild.me = make_member(bot=True, name="Mircewok")
    member = make_member(name="Andris")
    channel = make_channel(guild, members=[member], auto_finish=auto_finish, **channel_kwargs)
    if in_voice:
        member.voice = SimpleNamespace(channel=channel)
    return guild, member, channel, make_interaction(member, guild)


async def run(bot, name, interaction, **kwargs):
    await bot.tree.get_command(name).callback(interaction, **kwargs)


def last_message(interaction):
    return (interaction.response.messages or interaction.followup.messages)[-1]


def texts(interaction):
    return [m for kind, m in interaction.log if kind in ("response", "followup")]


# ------------------------------------------------------------------ /hang


async def test_hang_plays_in_the_users_channel(bot):
    guild, member, channel, interaction = scenario()
    await run(bot, "hang", interaction, nev="yasuo")
    reply = interaction.response.messages[0]
    assert "▶️" in reply["content"] and "**yasuo**" in reply["content"]
    assert channel.mention in reply["content"]
    assert reply["ephemeral"] is True
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert guild.voice_client.played[0].sound.name == "yasuo"
    assert interaction.followup.messages == []


async def test_hang_is_case_insensitive(bot):
    guild, _, _, interaction = scenario()
    await run(bot, "hang", interaction, nev="YASUO")
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert guild.voice_client.played[0].sound.name == "yasuo"


async def test_second_request_is_queued_with_position(bot):
    guild, member, channel, first = scenario(auto_finish=False)
    await run(bot, "hang", first, nev="yasuo")
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    second = make_interaction(member, guild)
    await run(bot, "hang", second, nev="talon")
    assert "⏳" in second.response.messages[0]["content"]
    assert "1 hang van előtte" in second.response.messages[0]["content"]


async def test_user_not_in_voice_gets_hint_and_nothing_is_queued(bot):
    guild, _, _, interaction = scenario(in_voice=False)
    await run(bot, "hang", interaction, nev="yasuo")
    assert "Előbb lépj be" in last_message(interaction)["content"]
    assert bot.players.peek(guild.id) is None


async def test_stage_channels_are_rejected(bot):
    guild, member, _, interaction = scenario()
    member.voice = SimpleNamespace(channel=MagicMock(spec=discord.StageChannel))
    await run(bot, "hang", interaction, nev="yasuo")
    assert "Stage" in last_message(interaction)["content"]


async def test_unknown_sound_suggests_alternatives(bot):
    _, _, _, interaction = scenario()
    await run(bot, "hang", interaction, nev="yasou")
    content = last_message(interaction)["content"]
    assert "Nincs ilyen hang" in content and "`yasuo`" in content and "/hangok" in content


async def test_unknown_sound_name_cannot_inject_markdown_or_mentions(bot):
    _, _, _, interaction = scenario()
    await run(bot, "hang", interaction, nev="**@everyone**")
    content = last_message(interaction)["content"]
    assert "**@everyone**" not in content
    assert bot.allowed_mentions.everyone is False


async def test_very_long_unknown_name_still_fits_in_a_discord_message(bot):
    """Discord akár 6000 karakteres szöveges opciót is enged; a válasz viszont max. 2000 lehet."""
    _, _, _, interaction = scenario()
    await run(bot, "hang", interaction, nev="x" * 5000)
    content = last_message(interaction)["content"]
    assert len(content) < 2000 and "Nincs ilyen hang" in content


def test_escape_markdown_neutralises_formatting_but_keeps_plain_text():
    assert escape_markdown("**a**_b_~c~`d`|e||") == r"\*\*a\*\*\_b\_\~c\~\`d\`\|e\|\|"
    assert escape_markdown("[x](y) # > -") == r"\[x\]\(y\) \# \> \-"
    assert escape_markdown("árvíztűrő tükörfúrógép 123") == "árvíztűrő tükörfúrógép 123"
    assert escape_markdown("") == ""


async def test_dm_usage_is_refused(bot):
    guild, member, _, interaction = scenario()
    interaction.guild = None
    await run(bot, "hang", interaction, nev="yasuo")
    assert "csak szerveren" in last_message(interaction)["content"]


async def test_queue_full_message(bot):
    guild, member, _, first = scenario(auto_finish=False)
    await run(bot, "hang", first, nev="yasuo")
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    for name in ("talon", "mario", "turret"):  # max_queue = 3
        await run(bot, "hang", make_interaction(member, guild), nev=name)
    overflow = make_interaction(member, guild)
    await run(bot, "hang", overflow, nev="lol")
    assert "Tele a lejátszási sor" in last_message(overflow)["content"]


async def test_async_failure_followup_arrives_after_the_initial_reply(bot):
    guild, _, _, interaction = scenario(connect_error=RuntimeError("PyNaCl library needed in order to use voice"))
    await run(bot, "hang", interaction, nev="yasuo")
    await wait_until(lambda: interaction.followup.messages)
    kinds = [kind for kind, _ in interaction.log]
    assert kinds == ["response", "followup"]
    assert "PyNaCl/davey" in interaction.followup.messages[0]["content"]
    assert interaction.followup.messages[0]["ephemeral"] is True


# --------------------------------------------------- jogosultságok, limit


def perms(**kwargs):
    base = dict(view_channel=True, connect=True, speak=True)
    base.update(kwargs)
    return discord.Permissions(**base)


async def test_missing_connect_permission_is_explained(bot):
    guild, _, channel, interaction = scenario(perms=perms(connect=False))
    await run(bot, "hang", interaction, nev="yasuo")
    assert "Nincs jogom csatlakozni" in last_message(interaction)["content"]
    assert channel.connect_calls == 0


async def test_missing_speak_permission_is_explained(bot):
    _, _, _, interaction = scenario(perms=perms(speak=False))
    await run(bot, "hang", interaction, nev="yasuo")
    assert "beszélni" in last_message(interaction)["content"]


def test_full_channel_is_detected_unless_bot_can_bypass():
    guild = FakeGuild()
    me = make_member(bot=True)
    full = make_channel(guild, members=[make_member(), make_member()], user_limit=2)
    assert "tele van" in check_channel_access(full, me)

    bypass = make_channel(
        guild, members=[make_member(), make_member()], user_limit=2, perms=perms(move_members=True)
    )
    assert check_channel_access(bypass, me) is None

    already_in = make_channel(guild, members=[make_member(), me], user_limit=2)
    assert check_channel_access(already_in, me) is None

    unlimited = make_channel(guild, members=[make_member() for _ in range(30)], user_limit=0)
    assert check_channel_access(unlimited, me) is None
    assert check_channel_access(full, None) is None


# ---------------------------------------------------------- /veletlen, /hangok


async def test_random_plays_some_known_sound(bot):
    guild, _, _, interaction = scenario()
    await run(bot, "veletlen", interaction)
    assert "▶️" in last_message(interaction)["content"]
    await wait_until(lambda: guild.voice_client and guild.voice_client.played)
    assert guild.voice_client.played[0].sound in set(bot.board)


async def test_hangok_lists_everything_within_embed_limits(bot):
    _, _, _, interaction = scenario()
    await run(bot, "hangok", interaction)
    message = interaction.response.messages[0]
    assert message["ephemeral"] is True
    embeds = message["embeds"]
    assert 1 <= len(embeds) <= 10
    assert "(71)" in embeds[0].title
    listing = " ".join(e.description for e in embeds)
    assert all(len(e.description) <= 4096 for e in embeds)
    for name in bot.board.names:
        assert f"`{name}`" in listing


def test_chunk_names_respects_limit_and_loses_nothing():
    names = [f"hang_{i:04d}" for i in range(2000)]
    chunks = chunk_names(names)
    assert len(chunks) > 1
    assert all(len(c) <= EMBED_DESCRIPTION_LIMIT for c in chunks)
    joined = ", ".join(chunks)
    assert all(f"`{n}`" in joined for n in names)
    assert joined.count("`") == 2 * len(names)


def test_chunk_names_sanitises_backticks_and_handles_empty():
    assert chunk_names([]) == []
    assert chunk_names(["a`b"]) == ["`a'b`"]


# ------------------------------------------------------------ /kihagy, /stop


async def start_playing(bot, guild, member, name="yasuo"):
    await run(bot, "hang", make_interaction(member, guild), nev=name)
    await wait_until(lambda: guild.voice_client and guild.voice_client.playing)
    return guild.voice_client


async def test_skip_by_listener(bot):
    guild, member, _, _ = scenario(auto_finish=False)
    vc = await start_playing(bot, guild, member)
    interaction = make_interaction(member, guild)
    await run(bot, "kihagy", interaction)
    assert "⏭️" in last_message(interaction)["content"]
    assert vc.stops == 1


async def test_skip_when_nothing_plays(bot):
    guild, member, _, interaction = scenario()
    await run(bot, "kihagy", interaction)
    assert "Most nem szól semmi" in last_message(interaction)["content"]


async def test_outsiders_cannot_skip_or_stop_but_moderators_can(bot):
    guild, member, channel, _ = scenario(auto_finish=False)
    vc = await start_playing(bot, guild, member)

    outsider = make_member(name="Kívülálló")
    for command in ("kihagy", "stop"):
        interaction = make_interaction(outsider, guild)
        await run(bot, command, interaction)
        assert "bot hangcsatornájában" in last_message(interaction)["content"]
    assert vc.playing and vc.disconnects == 0

    moderator = make_member(name="Moderátor")
    moderator.guild_permissions = discord.Permissions(move_members=True)
    interaction = make_interaction(moderator, guild)
    await run(bot, "kihagy", interaction)
    assert "⏭️" in last_message(interaction)["content"]


async def test_stop_defers_then_reports_dropped_items(bot):
    guild, member, _, _ = scenario(auto_finish=False)
    vc = await start_playing(bot, guild, member, "yasuo")
    for name in ("talon", "mario"):
        await run(bot, "hang", make_interaction(member, guild), nev=name)
    interaction = make_interaction(member, guild)
    await run(bot, "stop", interaction)
    assert interaction.log[0][0] == "defer"  # a 3 mp-es határidő miatt
    text = interaction.followup.messages[0]["content"]
    assert "⏹️" in text and "2 sorban álló hang törölve" in text
    assert vc.disconnects == 1 and guild.voice_client is None


async def test_stop_when_idle(bot):
    _, _, _, interaction = scenario()
    await run(bot, "stop", interaction)
    assert "Most nem játszom semmit" in last_message(interaction)["content"]


# ----------------------------------------------------------- hibakezelő


async def test_cooldown_error_is_friendly(bot):
    _, member, _, interaction = scenario()
    error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 2.0), 1.5)
    await bot.tree.on_error(interaction, error)
    assert "Lassabban" in last_message(interaction)["content"] and "1.5" in last_message(interaction)["content"]


async def test_unexpected_error_is_logged_and_user_gets_generic_message(bot, caplog):
    _, _, _, interaction = scenario()
    command = bot.tree.get_command("hang")
    interaction.command = command
    error = app_commands.CommandInvokeError(command, ValueError("boom"))
    with caplog.at_level(logging.ERROR):
        await bot.tree.on_error(interaction, error)
    assert "Váratlan hiba" in last_message(interaction)["content"]
    assert "boom" in caplog.text and "/hang" in caplog.text


async def test_error_after_initial_response_uses_followup(bot):
    _, _, _, interaction = scenario()
    await interaction.response.defer()
    await bot.tree.on_error(interaction, app_commands.NoPrivateMessage())
    assert interaction.followup.messages and "csak szerveren" in interaction.followup.messages[0]["content"]


async def test_error_message_failure_is_swallowed(bot, caplog):
    _, _, _, interaction = scenario()

    async def broken(*args, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=404, reason="Unknown interaction"), "lejárt")

    interaction.response.send_message = broken
    await bot.tree.on_error(interaction, app_commands.NoPrivateMessage())  # nem dobhat kivételt


# ------------------------------------------- parancsdefiníciók a Discord szerint


def payloads(bot):
    return {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}


def test_command_set(bot):
    assert set(payloads(bot)) == {"hang", "veletlen", "hangok", "kihagy", "stop"}


def test_payloads_respect_discord_limits(bot):
    import re

    for name, payload in payloads(bot).items():
        assert re.fullmatch(r"[a-z0-9_-]{1,32}", name)
        assert 1 <= len(payload["description"]) <= 100
        for option in payload.get("options", []):
            assert re.fullmatch(r"[a-z0-9_-]{1,32}", option["name"])
            assert 1 <= len(option["description"]) <= 100
    assert len(payloads(bot)) <= 100


def test_hang_has_required_string_option_with_autocomplete(bot):
    (option,) = payloads(bot)["hang"]["options"]
    assert option["name"] == "nev" and option["required"] is True
    assert option["type"] == discord.AppCommandOptionType.string.value
    assert option["autocomplete"] is True
    assert option["min_length"] == 1 and option["max_length"] == 100  # a hosszabb név úgysem létezhet
    assert "choices" not in option  # 71 hang > 25: csak autocomplete jöhet szóba


def test_voice_commands_are_guild_only(bot):
    for name in ("hang", "veletlen", "kihagy", "stop"):
        payload = payloads(bot)[name]
        assert payload["contexts"] == [0], name  # 0 = csak szerver


async def test_autocomplete_returns_valid_choices(bot):
    _, _, _, interaction = scenario()
    autocomplete = bot.tree.get_command("hang")._params["nev"].autocomplete
    everything = await autocomplete(interaction, "")
    assert len(everything) == 25
    for choice in everything:
        assert 0 < len(choice.name) <= 100 and choice.name == choice.value
    quinn = await autocomplete(interaction, "quinn")
    assert [c.value for c in quinn] == ["Quinnfeeder"]
    assert await autocomplete(interaction, "zzzzzzzz") == []
