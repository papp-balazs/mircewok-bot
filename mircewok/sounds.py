"""Az mp3 hangok katalógusa: beolvasás, névfeloldás, keresés, automatikus kiegészítés."""

from __future__ import annotations

import difflib
import logging
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Discord korlátok az autocomplete-ra: legfeljebb 25 találat, egy-egy név/érték max. 100 karakter.
MAX_CHOICES = 25
MAX_NAME_LENGTH = 100


@dataclass(frozen=True, slots=True)
class Sound:
    name: str  # a fájlnév kiterjesztés nélkül, eredeti írásmóddal
    path: Path  # abszolút útvonal


def _key(name: str) -> str:
    return name.strip().casefold()


class SoundBoard:
    """A `mp3/` mappa tartalma. A lejátszható hangot kizárólag ezen a katalóguson
    keresztül lehet kiválasztani, a felhasználó által beírt szöveg soha nem lesz
    közvetlenül fájlútvonal."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._sounds: dict[str, Sound] = {}
        self.reload()

    def reload(self) -> None:
        sounds: dict[str, Sound] = {}
        if not self.directory.is_dir():
            log.warning("A hangok mappája nem létezik: %s", self.directory)
        else:
            for path in sorted(self.directory.iterdir(), key=lambda p: p.name.casefold()):
                if not path.is_file() or path.suffix.lower() != ".mp3":
                    continue
                name = path.stem
                if not name.strip() or len(name) > MAX_NAME_LENGTH:
                    log.warning("Kihagyom a nem használható nevű fájlt: %s", path.name)
                    continue
                key = _key(name)
                if key in sounds:
                    log.warning(
                        "Kihagyom a(z) %s fájlt: a név (kis/nagybetűtől eltekintve) már foglalt: %s",
                        path.name,
                        sounds[key].path.name,
                    )
                    continue
                sounds[key] = Sound(name=name, path=path.resolve())
        self._sounds = sounds

    def __len__(self) -> int:
        return len(self._sounds)

    def __iter__(self) -> Iterator[Sound]:
        return iter(self._sounds.values())

    @property
    def names(self) -> list[str]:
        return [sound.name for sound in self._sounds.values()]

    def get(self, query: str) -> Sound | None:
        """Pontos név szerinti keresés, kis/nagybetű-függetlenül. A `.mp3` végződés elhagyható."""
        key = _key(query)
        found = self._sounds.get(key)
        if found is None and key.endswith(".mp3"):
            found = self._sounds.get(key[:-4].rstrip())
        return found

    def search(self, current: str, limit: int = MAX_CHOICES) -> list[Sound]:
        """Automatikus kiegészítés: előbb az előtaggal kezdődők, utána a részletet tartalmazók."""
        needle = _key(current)
        sounds = list(self._sounds.values())
        if not needle:
            return sounds[:limit]
        prefix = [s for s in sounds if _key(s.name).startswith(needle)]
        inside = [s for s in sounds if needle in _key(s.name) and s not in prefix]
        return (prefix + inside)[:limit]

    def suggest(self, query: str, count: int = 3) -> list[str]:
        """Javaslatok elírt névhez."""
        found = [s.name for s in self.search(query, limit=count)]
        if found:
            return found
        close = difflib.get_close_matches(_key(query), list(self._sounds), n=count, cutoff=0.5)
        return [self._sounds[key].name for key in close]

    def random(self) -> Sound:
        if not self._sounds:
            raise LookupError("Nincs betöltött hang.")
        return random.choice(list(self._sounds.values()))
