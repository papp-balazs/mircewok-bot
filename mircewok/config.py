"""Központi konfiguráció.

A bot MINDEN beállítása – benne a Discord-token – egyetlen helyről töltődik be:
a projekt gyökerében lévő `.env` fájlból, vagy ugyanilyen nevű környezeti
változókból (ezek elsőbbséget élveznek). A kód többi része soha nem olvas
közvetlenül környezeti változót vagy tokent, csak a `Config` objektumot kapja meg.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOUNDS_DIR = PROJECT_ROOT / "mp3"
ENV_FILE = PROJECT_ROOT / ".env"

# Ezt a helykitöltőt kell lecserélni a valódi tokenre a .env fájlban.
TOKEN_PLACEHOLDER = "<tokened-helye_a1b2c3d4e5f6g7h8i9>"

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class ConfigError(Exception):
    """Hibás vagy hiányzó beállítás; az üzenet közvetlenül a felhasználónak szól."""


@dataclass(frozen=True)
class Config:
    # A token repr-je szándékosan rejtett, hogy véletlenül se kerüljön naplóba.
    token: str = field(repr=False)
    ffmpeg: str
    sounds_dir: Path = DEFAULT_SOUNDS_DIR
    guild_id: int | None = None
    idle_timeout: float = 60.0
    max_queue: int = 10
    log_level: int = logging.INFO


def clean_token(raw: str | None, env_path: Path = ENV_FILE) -> str:
    """Ellenőrzi és megtisztítja a tokent; hiba esetén ConfigError-t dob.

    A hibaüzenetek soha nem tartalmazzák magát a tokent.
    """
    if raw is None:
        raise ConfigError(
            "Nincs megadva a DISCORD_TOKEN. Másold le a .env.example fájlt "
            f".env néven ({env_path}), és írd bele a bot tokenjét."
        )

    token = raw.strip()
    # Idézőjelek: DISCORD_TOKEN="abc" – a dotenv ezt kezeli, de környezeti
    # változóból érkező értéknél ez nem történik meg.
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    # Gyakori másolási hiba: a "Bot " előtag. A discord.py magától hozzáteszi.
    if token.lower().startswith("bot "):
        token = token[4:].strip()

    if not token or token == TOKEN_PLACEHOLDER or (token.startswith("<") and token.endswith(">")):
        raise ConfigError(
            "A DISCORD_TOKEN még a példa helykitöltő. Cseréld le a valódi tokenre a .env fájlban "
            "(Discord Developer Portal → Bot → Reset Token)."
        )
    if re.search(r"\s", token):
        raise ConfigError(
            "A DISCORD_TOKEN szóközt vagy sortörést tartalmaz. Ellenőrizd, hogy pontosan a "
            "token másolódott-e be, semmi más."
        )
    return token


def resolve_ffmpeg(configured: str | None = None) -> str:
    """Megkeresi az ffmpeg futtatható fájlt.

    Sorrend: FFMPEG_PATH beállítás → rendszer PATH → az `imageio-ffmpeg`
    csomag beépített példánya (így Windowson is telepítés nélkül működik).
    """
    if configured:
        found = shutil.which(configured)
        if found is None:
            raise ConfigError(
                f"Az FFMPEG_PATH ({configured}) nem létező vagy nem futtatható fájlra mutat."
            )
        return found

    found = shutil.which("ffmpeg")
    if found is not None:
        return found

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # hiányzó csomag vagy hiányzó binárisok
        raise ConfigError(
            "Nem található az ffmpeg. Telepítsd (pl. Windows: winget install ffmpeg, "
            "Debian/Ubuntu: apt install ffmpeg), vagy futtasd: pip install -r requirements.txt, "
            "esetleg add meg az útvonalát az FFMPEG_PATH beállításban."
        ) from exc


def _parse_number(raw: str | None, name: str, default, cast, minimum, maximum):
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        kind = "egész" if cast is int else "szám"
        raise ConfigError(f"A(z) {name} értéke nem {kind}: {raw!r}") from None
    if not (minimum <= value <= maximum):
        raise ConfigError(f"A(z) {name} értékének {minimum} és {maximum} között kell lennie.")
    return value


def load_config(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str | None = None,
) -> Config:
    """Betölti a beállításokat a .env fájlból és a környezeti változókból."""
    env = os.environ if environ is None else environ
    path = ENV_FILE if dotenv_path is None else Path(dotenv_path)
    file_values: dict[str, str] = {}
    if path.is_file():
        file_values = {k: v for k, v in dotenv_values(path).items() if v is not None}

    def get(key: str) -> str | None:
        for source in (env, file_values):
            value = source.get(key)
            if value is not None and value.strip():
                return value.strip()
        return None

    token = clean_token(get("DISCORD_TOKEN"), path)

    guild_id = _parse_number(get("GUILD_ID"), "GUILD_ID", None, int, 1, 2**63 - 1)
    idle_timeout = _parse_number(get("IDLE_TIMEOUT"), "IDLE_TIMEOUT", 60.0, float, 5.0, 3600.0)
    max_queue = _parse_number(get("MAX_QUEUE"), "MAX_QUEUE", 10, int, 1, 50)

    level_name = (get("LOG_LEVEL") or "INFO").upper()
    if level_name not in _LOG_LEVELS:
        raise ConfigError(
            f"Ismeretlen LOG_LEVEL: {level_name!r} (lehetséges: {', '.join(_LOG_LEVELS)})."
        )

    return Config(
        token=token,
        ffmpeg=resolve_ffmpeg(get("FFMPEG_PATH")),
        sounds_dir=DEFAULT_SOUNDS_DIR,
        guild_id=guild_id,
        idle_timeout=idle_timeout,
        max_queue=max_queue,
        log_level=_LOG_LEVELS[level_name],
    )
