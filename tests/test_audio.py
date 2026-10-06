"""Valódi ffmpeg-gel ellenőrzi, hogy MINDEN mp3 lejátszható hangforrássá alakul.

Ugyanazt a hangforrás-gyárat használja, mint az éles bot (`ffmpeg_source_factory`),
így a lejátszási folyamat utolsó, hálózat nélkül tesztelhető lépését is lefedi.
"""

import shutil

import discord
import pytest

from mircewok.config import DEFAULT_SOUNDS_DIR
from mircewok.player import MAX_CLIP_SECONDS, ffmpeg_source_factory
from mircewok.sounds import SoundBoard

FRAME_SECONDS = 0.02  # a Discord 20 ms-os csomagokat vár

SOUNDS = list(SoundBoard(DEFAULT_SOUNDS_DIR))


def _ffmpegs():
    found = {}
    system = shutil.which("ffmpeg")
    if system:
        found["rendszer-ffmpeg"] = system
    try:
        import imageio_ffmpeg

        found["beépített-ffmpeg"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return found


FFMPEGS = _ffmpegs()


def test_at_least_one_ffmpeg_is_available():
    assert FFMPEGS, "Sem rendszer, sem beépített ffmpeg nincs"


# A discord.py `FFmpegPCMAudio.cleanup()` a saját csövét ResourceWarning mellett zárja le; ez a
# könyvtár belső működése (a kimenet így is szabályosan felszabadul), nem a mi kódunk hibája.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("ffmpeg", FFMPEGS.values(), ids=FFMPEGS.keys())
@pytest.mark.parametrize("sound", SOUNDS, ids=[s.name for s in SOUNDS])
def test_every_mp3_decodes_to_discord_pcm(sound, ffmpeg):
    source = ffmpeg_source_factory(ffmpeg)(sound)
    assert isinstance(source, discord.AudioSource)
    assert not source.is_opus()
    try:
        frames = 0
        while True:
            chunk = source.read()
            if not chunk:
                break
            # 48 kHz, 2 csatorna, 16 bit, 20 ms = 3840 bájt
            assert len(chunk) == discord.opus.Encoder.FRAME_SIZE
            frames += 1
    finally:
        source.cleanup()
    duration = frames * FRAME_SECONDS
    assert 0.1 < duration < MAX_CLIP_SECONDS, f"{sound.name}: {duration:.1f} s"


def test_missing_file_yields_silence_not_a_crash(tmp_path):
    from mircewok.sounds import Sound

    ffmpeg = next(iter(FFMPEGS.values()))
    source = ffmpeg_source_factory(ffmpeg)(Sound("nincs", tmp_path / "nincs.mp3"))
    try:
        assert source.read() == b""
    finally:
        source.cleanup()
