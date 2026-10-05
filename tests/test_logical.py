"""Tests for logical path encoding/decoding."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from platformdirs.api import PlatformDirsABC
from platformdirs.logical import LogicalPathError, decode_path, encode_path


class FakePlatformDirs(PlatformDirsABC):  # ruff:ignore[too-many-public-methods]
    """Fake PlatformDirs with controlled roots for testing."""

    def __init__(self, config_root: str, data_root: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._config_root = config_root
        self._data_root = data_root

    @property
    def user_config_dir(self) -> str:
        return self._config_root

    @property
    def user_data_dir(self) -> str:
        return self._data_root

    @property
    def site_data_dir(self) -> str:
        return ""

    @property
    def site_config_dir(self) -> str:
        return ""

    @property
    def user_cache_dir(self) -> str:
        return ""

    @property
    def site_cache_dir(self) -> str:
        return ""

    @property
    def user_state_dir(self) -> str:
        return ""

    @property
    def site_state_dir(self) -> str:
        return ""

    @property
    def user_log_dir(self) -> str:
        return ""

    @property
    def site_log_dir(self) -> str:
        return ""

    @property
    def user_documents_dir(self) -> str:
        return ""

    @property
    def user_downloads_dir(self) -> str:
        return ""

    @property
    def user_pictures_dir(self) -> str:
        return ""

    @property
    def user_videos_dir(self) -> str:
        return ""

    @property
    def user_music_dir(self) -> str:
        return ""

    @property
    def user_desktop_dir(self) -> str:
        return ""

    @property
    def user_projects_dir(self) -> str:
        return ""

    @property
    def user_publicshare_dir(self) -> str:
        return ""

    @property
    def user_templates_dir(self) -> str:
        return ""

    @property
    def user_fonts_dir(self) -> str:
        return ""

    @property
    def user_preference_dir(self) -> str:
        return ""

    @property
    def user_bin_dir(self) -> str:
        return ""

    @property
    def site_bin_dir(self) -> str:
        return ""

    @property
    def user_applications_dir(self) -> str:
        return ""

    @property
    def site_applications_dir(self) -> str:
        return ""

    @property
    def user_runtime_dir(self) -> str:
        return ""

    @property
    def site_runtime_dir(self) -> str:
        return ""


WINDOWS_CONFIG_ROOT = r"C:\Users\testuser\AppData\Local\myapp"
WINDOWS_DATA_ROOT = r"C:\Users\testuser\AppData\Roaming\myapp"
POSIX_CONFIG_ROOT = "/home/testuser/.config/myapp"
POSIX_DATA_ROOT = "/home/testuser/.local/share/myapp"


@pytest.fixture
def windows_dirs() -> FakePlatformDirs:
    return FakePlatformDirs(WINDOWS_CONFIG_ROOT, WINDOWS_DATA_ROOT)


@pytest.fixture
def posix_dirs() -> FakePlatformDirs:
    return FakePlatformDirs(POSIX_CONFIG_ROOT, POSIX_DATA_ROOT)


def test_config_roundtrip_windows(windows_dirs: FakePlatformDirs) -> None:
    path = str(Path(WINDOWS_CONFIG_ROOT) / "settings.toml")
    encoded = encode_path(windows_dirs, path)
    assert encoded == "config:settings.toml"
    decoded = decode_path(windows_dirs, encoded)
    assert decoded == Path(path)


def test_config_roundtrip_posix(posix_dirs: FakePlatformDirs) -> None:
    path = str(PurePosixPath(POSIX_CONFIG_ROOT) / "settings.toml")
    encoded = encode_path(posix_dirs, path)
    assert encoded == "config:settings.toml"
    decoded = decode_path(posix_dirs, encoded)
    assert decoded == Path(path)


def test_data_roundtrip_windows(windows_dirs: FakePlatformDirs) -> None:
    path = str(Path(WINDOWS_DATA_ROOT) / "data.db")
    encoded = encode_path(windows_dirs, path)
    assert encoded == "data:data.db"
    decoded = decode_path(windows_dirs, encoded)
    assert decoded == Path(path)


def test_data_roundtrip_posix(posix_dirs: FakePlatformDirs) -> None:
    path = str(PurePosixPath(POSIX_DATA_ROOT) / "data.db")
    encoded = encode_path(posix_dirs, path)
    assert encoded == "data:data.db"
    decoded = decode_path(posix_dirs, encoded)
    assert decoded == Path(path)


def test_nested_segments_roundtrip(posix_dirs: FakePlatformDirs) -> None:
    path = str(PurePosixPath(POSIX_CONFIG_ROOT) / "sub" / "dir" / "settings.toml")
    encoded = encode_path(posix_dirs, path)
    assert encoded == "config:sub/dir/settings.toml"
    decoded = decode_path(posix_dirs, encoded)
    assert decoded == Path(path)


def test_cross_machine_config(windows_dirs: FakePlatformDirs, posix_dirs: FakePlatformDirs) -> None:
    win_path = str(Path(WINDOWS_CONFIG_ROOT) / "settings.toml")
    encoded = encode_path(windows_dirs, win_path)
    assert encoded == "config:settings.toml"
    posix_decoded = decode_path(posix_dirs, encoded)
    assert posix_decoded == Path(POSIX_CONFIG_ROOT) / "settings.toml"
    win_decoded = decode_path(windows_dirs, encoded)
    assert win_decoded == Path(win_path)


def test_cross_machine_data(windows_dirs: FakePlatformDirs, posix_dirs: FakePlatformDirs) -> None:
    posix_path = str(PurePosixPath(POSIX_DATA_ROOT) / "app.db")
    encoded = encode_path(posix_dirs, posix_path)
    assert encoded == "data:app.db"
    win_decoded = decode_path(windows_dirs, encoded)
    assert win_decoded == Path(WINDOWS_DATA_ROOT) / "app.db"
    posix_decoded = decode_path(posix_dirs, encoded)
    assert posix_decoded == Path(posix_path)


def test_a_vs_ab_not_mixed() -> None:
    dirs = FakePlatformDirs("/root/a", "/root/data")
    path = "/root/abc/file.txt"
    encoded = encode_path(dirs, path)
    assert encoded.startswith("external:")


def test_ab_root_matches() -> None:
    dirs = FakePlatformDirs("/root/ab", "/root/data")
    path = "/root/ab/file.txt"
    encoded = encode_path(dirs, path)
    assert encoded == "config:file.txt"


def test_longest_root_wins() -> None:
    dirs = FakePlatformDirs("/root/config", "/root/config/data")
    path = "/root/config/data/file.txt"
    encoded = encode_path(dirs, path)
    assert encoded == "data:file.txt"


def test_encode_dotdot_rejected(posix_dirs: FakePlatformDirs) -> None:
    path = str(PurePosixPath(POSIX_CONFIG_ROOT) / ".." / "escape.txt")
    with pytest.raises(LogicalPathError, match="escape attempt"):
        encode_path(posix_dirs, path)


def test_decode_dotdot_rejected(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="escape attempt"):
        decode_path(posix_dirs, "config:../escape.txt")


def test_decode_dot_rejected(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="not allowed"):
        decode_path(posix_dirs, "config:./settings.toml")


def test_decode_hidden_separator_rejected(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="hidden separator"):
        decode_path(posix_dirs, "config:sub\\dir/settings.toml")


def test_decode_null_byte_rejected(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="null byte"):
        decode_path(posix_dirs, "config:sub\x00dir/settings.toml")


def test_same_root_aliased_rejected() -> None:
    dirs = FakePlatformDirs("/root/same", "/root/same")
    path = "/root/same/settings.toml"
    with pytest.raises(LogicalPathError, match="aliased root"):
        encode_path(dirs, path)


def test_case_insensitive_alias_rejected() -> None:
    dirs = FakePlatformDirs("C:\\Root\\Same", "c:\\root\\same")
    path = "C:\\Root\\Same\\settings.toml"
    with pytest.raises(LogicalPathError, match="aliased root"):
        encode_path(dirs, path)


def test_external_path_encoded(posix_dirs: FakePlatformDirs) -> None:
    path = "/some/external/path.txt"
    encoded = encode_path(posix_dirs, path)
    assert encoded == f"external:{path}"


def test_external_path_decoded_as_is(posix_dirs: FakePlatformDirs) -> None:
    encoded = "external:/some/external/path.txt"
    decoded = decode_path(posix_dirs, encoded)
    assert decoded == Path("/some/external/path.txt")


def test_external_path_not_portable(windows_dirs: FakePlatformDirs, posix_dirs: FakePlatformDirs) -> None:
    win_path = "C:\\some\\external\\path.txt"
    encoded = encode_path(windows_dirs, win_path)
    assert encoded.startswith("external:")
    posix_decoded = decode_path(posix_dirs, encoded)
    assert posix_decoded == Path(win_path)


def test_no_directory_created_on_encode(tmp_path: Path) -> None:
    dirs = FakePlatformDirs(str(tmp_path / "config"), str(tmp_path / "data"))
    path = str(tmp_path / "config" / "settings.toml")
    encode_path(dirs, path)
    assert not (tmp_path / "config").exists()


def test_no_directory_created_on_decode(tmp_path: Path) -> None:
    dirs = FakePlatformDirs(str(tmp_path / "config"), str(tmp_path / "data"))
    encoded = "config:settings.toml"
    decoded = decode_path(dirs, encoded)
    assert not (tmp_path / "config").exists()
    assert decoded == Path(tmp_path / "config" / "settings.toml")


def test_missing_role_separator(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="role separator"):
        decode_path(posix_dirs, "settings.toml")


def test_unknown_role(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="unknown role"):
        decode_path(posix_dirs, "cache:settings.toml")


def test_empty_role(posix_dirs: FakePlatformDirs) -> None:
    with pytest.raises(LogicalPathError, match="unknown role"):
        decode_path(posix_dirs, ":settings.toml")


def test_empty_segments(posix_dirs: FakePlatformDirs) -> None:
    decoded = decode_path(posix_dirs, "config:")
    assert decoded == Path(POSIX_CONFIG_ROOT)
