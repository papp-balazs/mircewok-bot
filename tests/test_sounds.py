import pytest

from mircewok.config import DEFAULT_SOUNDS_DIR
from mircewok.sounds import MAX_CHOICES, MAX_NAME_LENGTH, SoundBoard


@pytest.fixture(scope="module")
def real_board():
    return SoundBoard(DEFAULT_SOUNDS_DIR)


def make_dir(tmp_path, names):
    for name in names:
        (tmp_path / name).write_bytes(b"ID3")
    return SoundBoard(tmp_path)


# ------------------------------------------------------- a valódi mp3 mappa


def test_all_71_real_sounds_are_loaded(real_board):
    assert len(real_board) == 71


def test_real_names_are_unique_and_within_discord_limits(real_board):
    names = real_board.names
    assert len({n.casefold() for n in names}) == len(names)
    assert all(0 < len(n) <= MAX_NAME_LENGTH for n in names)


def test_real_paths_are_absolute_files(real_board):
    for sound in real_board:
        assert sound.path.is_absolute() and sound.path.is_file()


def test_lookup_is_case_insensitive_and_accepts_extension(real_board):
    expected = real_board.get("Quinnfeeder")
    assert expected is not None and expected.name == "Quinnfeeder"
    assert real_board.get("quinnfeeder") is expected
    assert real_board.get("  QUINNFEEDER.MP3 ") is expected


def test_unknown_lookup_returns_none(real_board):
    assert real_board.get("nincsilyen") is None
    assert real_board.get("") is None
    assert real_board.get("../../etc/passwd") is None


def test_path_traversal_never_reaches_the_filesystem(real_board):
    for evil in ["../README", "..\\README", "/etc/passwd", "mp3/../README.md", "-i x", "pipe:0"]:
        assert real_board.get(evil) is None


def test_autocomplete_is_capped_at_25_and_ordered(real_board):
    assert len(real_board.search("")) == MAX_CHOICES
    assert len(real_board.search("a")) <= MAX_CHOICES


def test_autocomplete_prefix_matches_come_first(real_board):
    results = [s.name.casefold() for s in real_board.search("ne")]
    prefix_count = sum(1 for n in results if n.startswith("ne"))
    assert results[:prefix_count] == [n for n in results if n.startswith("ne")]
    assert prefix_count >= 1


def test_autocomplete_finds_substrings_case_insensitively(real_board):
    assert any(s.name == "Quinnfeeder" for s in real_board.search("FEED"))


def test_suggest_helps_with_typos(real_board):
    assert "yasuo" in real_board.suggest("yasou")
    assert real_board.suggest("zzzzzzzzzz") == []


def test_random_returns_a_known_sound(real_board):
    assert real_board.random() in set(real_board)


# --------------------------------------------------------- szélsőséges esetek


def test_only_mp3_files_are_loaded_and_subdirectories_ignored(tmp_path):
    (tmp_path / "dir.mp3").mkdir()
    board = make_dir(tmp_path, ["a.mp3", "b.MP3", "c.wav", "d.txt", "e.mp3.bak"])
    assert sorted(board.names) == ["a", "b"]


def test_case_colliding_names_keep_only_the_first(tmp_path, caplog):
    board = make_dir(tmp_path, ["Foo.mp3", "foo.mp3"])
    assert len(board) == 1
    assert "már foglalt" in caplog.text


def test_overlong_names_are_skipped(tmp_path):
    board = make_dir(tmp_path, ["x" * (MAX_NAME_LENGTH + 1) + ".mp3", "ok.mp3"])
    assert board.names == ["ok"]


def test_missing_directory_gives_empty_board(tmp_path, caplog):
    board = SoundBoard(tmp_path / "nincs")
    assert len(board) == 0
    assert "nem létezik" in caplog.text
    with pytest.raises(LookupError):
        board.random()


def test_reload_picks_up_new_files(tmp_path):
    board = make_dir(tmp_path, ["a.mp3"])
    (tmp_path / "b.mp3").write_bytes(b"ID3")
    board.reload()
    assert sorted(board.names) == ["a", "b"]


def test_hungarian_accented_names_work(tmp_path):
    board = make_dir(tmp_path, ["árvíztűrő.mp3"])
    assert board.get("ÁRVÍZTŰRŐ").name == "árvíztűrő"
