"""Indítás: konfiguráció, előfeltételek ellenőrzése, a bot futtatása."""

from __future__ import annotations

import importlib.util
import logging
import sys

import aiohttp
import discord

from .client import MircewokBot
from .config import ConfigError, load_config
from .sounds import SoundBoard

log = logging.getLogger(__name__)


def voice_problems() -> list[str]:
    """Felsorolja, mi hiányzik a hanghoz. Üres lista = minden rendben.

    A Discord 2026 márciusa óta minden hanghívásban kötelezővé tette a DAVE
    (végpontok közti) titkosítást, ezért a `davey` csomag is elengedhetetlen.
    """
    problems: list[str] = []
    if importlib.util.find_spec("nacl") is None:
        problems.append("hiányzik a PyNaCl csomag (pip install -r requirements.txt)")
    if importlib.util.find_spec("davey") is None:
        problems.append(
            "hiányzik a davey csomag, enélkül a mai Discord nem enged hangot "
            "(pip install -r requirements.txt)"
        )
    try:
        discord.opus.Encoder()  # szükség esetén betölti a libopust
    except discord.opus.OpusNotLoaded:
        problems.append("nem tölthető be a libopus (Linuxon: apt install libopus0)")
    except Exception as exc:  # pragma: no cover - védelmi háló
        problems.append(f"az Opus kódoló nem inicializálható: {exc!r}")
    return problems


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Konfigurációs hiba: {exc}", file=sys.stderr)
        return 2

    board = SoundBoard(config.sounds_dir)
    if len(board) == 0:
        print(f"Nincs egyetlen .mp3 hang sem itt: {config.sounds_dir}", file=sys.stderr)
        return 2

    problems = voice_problems()
    if problems:
        print("A hangfunkciók nem működnének:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    bot = MircewokBot(config, board)
    try:
        # A run() kezeli a naplózást és a Ctrl+C-t is; root_logger=True, hogy a
        # saját moduljaink üzenetei is megjelenjenek, ne csak a discord.py-éi.
        bot.run(config.token, log_level=config.log_level, root_logger=True)
    except discord.LoginFailure:
        print(
            "A Discord elutasította a tokent. Ellenőrizd a DISCORD_TOKEN értékét a .env fájlban; "
            "ha kiszivárgott vagy lejárt, generálj újat a Developer Portalon (Bot → Reset Token).",
            file=sys.stderr,
        )
        return 1
    except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
        # Nincs internet, a Discord elérhetetlen vagy tűzfal/proxy blokkolja a kapcsolatot.
        print(
            f"Nem sikerült kapcsolódni a Discordhoz ({type(exc).__name__}: {exc}). "
            "Ellenőrizd az internetkapcsolatot, a tűzfalat/proxyt, és hogy a discord.com elérhető-e.",
            file=sys.stderr,
        )
        return 1
    return 0
