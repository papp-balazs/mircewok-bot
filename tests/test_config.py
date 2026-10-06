import logging
import shutil

import pytest

from mircewok import config
from mircewok.config import (
    TOKEN_PLACEHOLDER,
    ConfigError,
    clean_token,
    load_config,
    resolve_ffmpeg,
)

VALID = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch):
    """A config-tesztek ne függjenek a gép ffmpeg-jétől."""
    monkeypatch.setattr(config, "resolve_ffmpeg", lambda configured=None: configured or "/usr/bin/ffmpeg")


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- token


def test_placeholder_token_is_rejected():
    with pytest.raises(ConfigError, match="helykitöltő"):
        clean_token(TOKEN_PLACEHOLDER)


@pytest.mark.parametrize("raw", ["<valami>", "<TOKEN>", "  <tokened-helye_a1b2c3d4e5f6g7h8i9>  "])
def test_any_angle_bracket_placeholder_is_rejected(raw):
    with pytest.raises(ConfigError):
        clean_token(raw)


@pytest.mark.parametrize("raw", [None])
def test_missing_token_gives_setup_hint(raw):
    with pytest.raises(ConfigError, match=r"\.env\.example"):
        clean_token(raw)


@pytest.mark.parametrize("raw", ["", "   ", '""', "''"])
def test_empty_token_is_rejected(raw):
    with pytest.raises(ConfigError):
        clean_token(raw)


def test_token_with_whitespace_inside_is_rejected():
    with pytest.raises(ConfigError, match="szóközt"):
        clean_token("abc def.ghi.jkl")


@pytest.mark.parametrize(
    "raw",
    [VALID, f"  {VALID}  ", f'"{VALID}"', f"'{VALID}'", f"Bot {VALID}", f'"Bot {VALID}"', f"bot {VALID}"],
)
def test_token_is_normalised(raw):
    assert clean_token(raw) == VALID


def test_error_messages_never_contain_the_token():
    with pytest.raises(ConfigError) as err:
        clean_token("secret part.with space.xx")
    assert "secret" not in str(err.value)


def test_config_repr_hides_token(tmp_path):
    cfg = load_config({"DISCORD_TOKEN": VALID}, tmp_path / "nincs.env")
    assert VALID not in repr(cfg)


# ------------------------------------------------------- betöltés / .env


def test_shipped_env_example_contains_the_placeholder_and_is_rejected():
    """Az `.env.example` pontosan a kért helykitöltőt tartalmazza, és így nem indul el élesben."""
    example = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"DISCORD_TOKEN={TOKEN_PLACEHOLDER}" in example
    with pytest.raises(ConfigError, match="helykitöltő"):
        load_config({}, config.PROJECT_ROOT / ".env.example")


def test_token_from_dotenv_file(tmp_path):
    cfg = load_config({}, write_env(tmp_path, f"DISCORD_TOKEN={VALID}\n"))
    assert cfg.token == VALID


def test_quoted_dotenv_token(tmp_path):
    cfg = load_config({}, write_env(tmp_path, f'DISCORD_TOKEN="{VALID}"\n'))
    assert cfg.token == VALID


def test_dotenv_with_placeholder_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="helykitöltő"):
        load_config({}, write_env(tmp_path, f"DISCORD_TOKEN={TOKEN_PLACEHOLDER}\n"))


def test_environment_variable_beats_dotenv(tmp_path):
    path = write_env(tmp_path, f"DISCORD_TOKEN={TOKEN_PLACEHOLDER}\n")
    assert load_config({"DISCORD_TOKEN": VALID}, path).token == VALID


def test_empty_environment_variable_falls_back_to_dotenv(tmp_path):
    path = write_env(tmp_path, f"DISCORD_TOKEN={VALID}\n")
    assert load_config({"DISCORD_TOKEN": ""}, path).token == VALID


def test_missing_dotenv_and_env_gives_error(tmp_path):
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config({}, tmp_path / "nincs.env")


def test_defaults(tmp_path):
    cfg = load_config({"DISCORD_TOKEN": VALID}, tmp_path / "nincs.env")
    assert cfg.guild_id is None
    assert cfg.idle_timeout == 60.0
    assert cfg.max_queue == 10
    assert cfg.log_level == logging.INFO
    assert cfg.sounds_dir == config.PROJECT_ROOT / "mp3"


def test_optional_settings_are_parsed(tmp_path):
    env = {
        "DISCORD_TOKEN": VALID,
        "GUILD_ID": "123456789012345678",
        "IDLE_TIMEOUT": "90.5",
        "MAX_QUEUE": "3",
        "LOG_LEVEL": "debug",
    }
    cfg = load_config(env, tmp_path / "nincs.env")
    assert cfg.guild_id == 123456789012345678
    assert cfg.idle_timeout == 90.5
    assert cfg.max_queue == 3
    assert cfg.log_level == logging.DEBUG


@pytest.mark.parametrize(
    "key,value",
    [
        ("GUILD_ID", "abc"),
        ("GUILD_ID", "0"),
        ("GUILD_ID", "-5"),
        ("IDLE_TIMEOUT", "gyors"),
        ("IDLE_TIMEOUT", "1"),
        ("MAX_QUEUE", "0"),
        ("MAX_QUEUE", "999"),
        ("MAX_QUEUE", "2.5"),
        ("LOG_LEVEL", "csacsogó"),
    ],
)
def test_invalid_optional_settings_fail_with_clear_error(tmp_path, key, value):
    with pytest.raises(ConfigError, match=key):
        load_config({"DISCORD_TOKEN": VALID, key: value}, tmp_path / "nincs.env")


# --------------------------------------------------------------- ffmpeg


def test_resolve_ffmpeg_explicit_path_must_exist(monkeypatch):
    monkeypatch.undo()  # az autouse hamisítás nem kell
    with pytest.raises(ConfigError, match="FFMPEG_PATH"):
        resolve_ffmpeg("/nincs/ilyen/ffmpeg")


def test_resolve_ffmpeg_prefers_system_binary(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/ffmpeg" if name == "ffmpeg" else None)
    assert resolve_ffmpeg() == "/opt/ffmpeg"


def test_resolve_ffmpeg_falls_back_to_bundled(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    import imageio_ffmpeg

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "/bundled/ffmpeg")
    assert resolve_ffmpeg() == "/bundled/ffmpeg"


def test_resolve_ffmpeg_fails_with_install_hint(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    import imageio_ffmpeg

    def boom():
        raise RuntimeError("nincs bináris")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", boom)
    with pytest.raises(ConfigError, match="ffmpeg"):
        resolve_ffmpeg()
