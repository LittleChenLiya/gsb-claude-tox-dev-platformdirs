from __future__ import annotations

import os
from pathlib import Path

import pytest

from platformdirs import PlatformDirs
from platformdirs.portable import (
    AmbiguousRootError,
    ExternalRef,
    InvalidPortableRefError,
    PathNotPortableError,
    PortableCodec,
    PortableRef,
)

BACKSLASH = chr(92)


class FakeDirs:
    """Minimal PlatformDirs-like stand-in exposing only ``user_<role>_dir`` strings."""

    def __init__(self, roots: dict[str, str]) -> None:
        for role, root in roots.items():
            setattr(self, f"user_{role}_dir", root)


WIN_ROOTS = {
    "config": "C:/Users/win/AppData/Roaming/Acme",
    "data": "C:/Users/win/AppData/Local/Acme",
    "cache": "C:/Users/win/AppData/Local/Acme/Cache",
    "state": "C:/Users/win/AppData/Local/Acme/State",
    "log": "C:/Users/win/AppData/Local/Acme/Logs",
}

POSIX_ROOTS = {
    "config": "/home/tux/.config/Acme",
    "data": "/home/tux/.local/share/Acme",
    "cache": "/home/tux/.cache/Acme",
    "state": "/home/tux/.local/state/Acme",
    "log": "/home/tux/.local/state/Acme/logs",
}


@pytest.fixture
def win_codec() -> PortableCodec:
    return PortableCodec(FakeDirs(WIN_ROOTS))


@pytest.fixture
def posix_codec() -> PortableCodec:
    return PortableCodec(FakeDirs(POSIX_ROOTS))


@pytest.mark.parametrize("role", sorted(WIN_ROOTS))
def test_cross_machine_roundtrip_windows_to_posix(
    win_codec: PortableCodec, posix_codec: PortableCodec, role: str
) -> None:
    local = Path(WIN_ROOTS[role]) / "nested" / "file.toml"
    ref = win_codec.encode(local)
    assert ref.role == role
    assert ref.segments == ("nested", "file.toml")
    assert ref.portable
    payload = ref.to_payload()
    rebound = posix_codec.decode(payload)
    assert rebound == Path(POSIX_ROOTS[role]) / "nested" / "file.toml"


@pytest.mark.parametrize("role", sorted(POSIX_ROOTS))
def test_cross_machine_roundtrip_posix_to_windows(
    posix_codec: PortableCodec, win_codec: PortableCodec, role: str
) -> None:
    local = Path(POSIX_ROOTS[role]) / "nested" / "file.toml"
    payload = posix_codec.encode(local).to_payload()
    assert win_codec.decode(payload) == Path(WIN_ROOTS[role]) / "nested" / "file.toml"


def test_encode_root_itself_gives_empty_segments(win_codec: PortableCodec) -> None:
    ref = win_codec.encode(WIN_ROOTS["config"])
    assert ref == PortableRef(role="config", segments=())
    assert ref.to_payload() == "config:"
    assert win_codec.decode("config:") == Path(WIN_ROOTS["config"])


def test_sibling_roots_a_and_ab_are_not_confused() -> None:
    codec = PortableCodec(
        FakeDirs({
            "config": "C:/roots/a",
            "data": "C:/roots/ab",
            "cache": "C:/roots/other/cache",
            "state": "C:/roots/other/state",
            "log": "C:/roots/other/log",
        })
    )
    assert codec.encode("C:/roots/a/conf.toml").role == "config"
    assert codec.encode("C:/roots/ab/data.bin").role == "data"
    # String-wise these start with the "a"/"ab" roots, but segment-wise they are outside every root.
    for outsider in ("C:/roots/abc/f", "C:/roots/a2/f", "C:/roots/ab2/f"):
        with pytest.raises(PathNotPortableError):
            codec.encode(outsider)


def test_nested_root_wins_by_longest_match(win_codec: PortableCodec) -> None:
    # The cache root lives *inside* the data root; the longer root must win.
    ref = win_codec.encode(Path(WIN_ROOTS["cache"]) / "thumbs" / "a.png")
    assert ref.role == "cache"
    assert ref.segments == ("thumbs", "a.png")
    ref = win_codec.encode(Path(WIN_ROOTS["data"]) / "db.sqlite")
    assert ref.role == "data"
    assert ref.segments == ("db.sqlite",)


def test_posix_nested_log_root_wins_over_state(posix_codec: PortableCodec) -> None:
    ref = posix_codec.encode(Path(POSIX_ROOTS["log"]) / "app.log")
    assert ref.role == "log"
    assert ref.segments == ("app.log",)


@pytest.mark.parametrize(
    "payload",
    [
        "config:../../etc/passwd",
        "config:a/../../b",
        "config:..",
        "config:.",
        "config:a//b",
        "config:a/./b",
        "config:C:/evil",
        "config:" + BACKSLASH + ".." + BACKSLASH + "evil",
        "config:a" + BACKSLASH + "b",
        "data:C:" + BACKSLASH + "Windows",
        "config:a" + chr(0) + "b",
    ],
)
def test_decode_rejects_escaping_or_hidden_separator_payloads(win_codec: PortableCodec, payload: str) -> None:
    with pytest.raises(InvalidPortableRefError):
        win_codec.decode(payload)


def test_ref_construction_rejects_bad_segments() -> None:
    with pytest.raises(InvalidPortableRefError):
        PortableRef(role="config", segments=("..", "evil"))
    with pytest.raises(InvalidPortableRefError):
        PortableRef(role="config", segments=("a", "", "b"))
    with pytest.raises(InvalidPortableRefError):
        PortableRef(role="config", segments=("a", "b" + BACKSLASH + "c"))
    with pytest.raises(InvalidPortableRefError):
        PortableRef(role="not a role", segments=())


def test_decode_rejects_garbage(win_codec: PortableCodec) -> None:
    with pytest.raises(InvalidPortableRefError):
        win_codec.decode("no-colon-here")
    with pytest.raises(InvalidPortableRefError):
        win_codec.decode("unknownrole:x")
    with pytest.raises(InvalidPortableRefError):
        win_codec.decode(ExternalRef(path="C:/elsewhere"))


def test_ambiguous_aliased_roots_are_rejected() -> None:
    codec = PortableCodec(
        FakeDirs({
            "config": "C:/same/root",
            "data": "C:/same/root",
            "cache": "C:/same/root/cache",
            "state": "C:/other/state",
            "log": "C:/other/log",
        })
    )
    with pytest.raises(AmbiguousRootError, match="aliased roots"):
        codec.encode("C:/same/root/settings.toml")
    # Non-aliased roots of the same codec still work.
    assert codec.encode("C:/other/state/x.bin").role == "state"


def test_external_path_is_marked_not_portable(win_codec: PortableCodec, posix_codec: PortableCodec) -> None:
    marked = win_codec.try_encode("D:/outside/file.txt")
    assert isinstance(marked, ExternalRef)
    assert not marked.portable
    assert marked.path == "D:/outside/file.txt"
    marked_posix = posix_codec.try_encode("/etc/passwd")
    assert isinstance(marked_posix, ExternalRef)
    assert not marked_posix.portable
    with pytest.raises(PathNotPortableError):
        win_codec.encode("D:/outside/file.txt")


def test_codec_creates_no_directories_and_moves_no_files(tmp_path: Path) -> None:
    roots = {role: str(tmp_path / "roots" / role) for role in ("config", "data", "cache", "state", "log")}
    codec = PortableCodec(FakeDirs(roots))
    target = codec.decode("config:sub/dir/settings.toml")
    assert target == Path(roots["config"]) / "sub" / "dir" / "settings.toml"
    assert not (tmp_path / "roots").exists()  # decode is pure path arithmetic
    codec.encode(codec.root_for("data") / "x.bin")  # encode must not create anything either
    assert not (tmp_path / "roots").exists()


def test_payload_roundtrip() -> None:
    ref = PortableRef(role="data", segments=("app", "settings.toml"))
    assert ref.to_payload() == "data:app/settings.toml"
    assert PortableRef.from_payload("data:app/settings.toml") == ref
    assert str(ref) == "data:app/settings.toml"


def test_real_platformdirs_roundtrip() -> None:
    dirs = PlatformDirs("Acme", "AcmeCorp")
    codec = PortableCodec(dirs, roles=("cache",))  # single role: immune to platform root aliasing
    local = Path(dirs.user_cache_dir) / "thumbs" / "a.png"
    ref = codec.encode(local)
    assert ref.role == "cache"
    assert ref.segments == ("thumbs", "a.png")
    assert codec.decode(ref.to_payload()) == local


def test_roles_and_roots_listing(win_codec: PortableCodec) -> None:
    assert win_codec.roles == ("config", "data", "cache", "state", "log")
    assert dict(win_codec.iter_roots())["config"] == Path(WIN_ROOTS["config"])
    with pytest.raises(InvalidPortableRefError):
        win_codec.root_for("nope")


def test_missing_root_attribute_raises() -> None:
    with pytest.raises(AttributeError, match="user_config"):
        PortableCodec(FakeDirs({}), roles=("config",))


def test_os_pathlike_input_accepted(win_codec: PortableCodec) -> None:
    ref = win_codec.encode(Path(WIN_ROOTS["data"]) / "f.bin")
    assert ref.role == "data"
    assert os.fspath(win_codec.decode(ref)) == os.fspath(Path(WIN_ROOTS["data"]) / "f.bin")
