"""Logical path encoding/decoding based on PlatformDirs.

Encodes local config/data paths as role + segment sequences for portability across machines, and decodes them by
rebinding to the current machine's roots.

The encoded form is a string like ``config:app/settings.toml`` where:

- ``config`` is the role (``config`` or ``data``)
- ``app/settings.toml`` are the path segments (POSIX-style separators)

External paths (not under any known root) are encoded as ``external:<abspath>`` and are not portable across machines.

Cross-machine example:

    >>> from platformdirs import encode_path, decode_path
    >>> from platformdirs.api import PlatformDirsABC
    >>> class FakeDirs(PlatformDirsABC):  # ruff:ignore[too-many-public-methods]
    ...     def __init__(self, config_root, data_root, **kwargs):
    ...         super().__init__(**kwargs)
    ...         self._config_root = config_root
    ...         self._data_root = data_root
    ...     @property
    ...     def user_config_dir(self):
    ...         return self._config_root
    ...     @property
    ...     def user_data_dir(self):
    ...         return self._data_root
    ...     @property
    ...     def site_data_dir(self):
    ...         return ""
    ...     @property
    ...     def site_config_dir(self):
    ...         return ""
    ...     @property
    ...     def user_cache_dir(self):
    ...         return ""
    ...     @property
    ...     def site_cache_dir(self):
    ...         return ""
    ...     @property
    ...     def user_state_dir(self):
    ...         return ""
    ...     @property
    ...     def site_state_dir(self):
    ...         return ""
    ...     @property
    ...     def user_log_dir(self):
    ...         return ""
    ...     @property
    ...     def site_log_dir(self):
    ...         return ""
    ...     @property
    ...     def user_documents_dir(self):
    ...         return ""
    ...     @property
    ...     def user_downloads_dir(self):
    ...         return ""
    ...     @property
    ...     def user_pictures_dir(self):
    ...         return ""
    ...     @property
    ...     def user_videos_dir(self):
    ...         return ""
    ...     @property
    ...     def user_music_dir(self):
    ...         return ""
    ...     @property
    ...     def user_desktop_dir(self):
    ...         return ""
    ...     @property
    ...     def user_projects_dir(self):
    ...         return ""
    ...     @property
    ...     def user_publicshare_dir(self):
    ...         return ""
    ...     @property
    ...     def user_templates_dir(self):
    ...         return ""
    ...     @property
    ...     def user_fonts_dir(self):
    ...         return ""
    ...     @property
    ...     def user_preference_dir(self):
    ...         return ""
    ...     @property
    ...     def user_bin_dir(self):
    ...         return ""
    ...     @property
    ...     def site_bin_dir(self):
    ...         return ""
    ...     @property
    ...     def user_applications_dir(self):
    ...         return ""
    ...     @property
    ...     def site_applications_dir(self):
    ...         return ""
    ...     @property
    ...     def user_runtime_dir(self):
    ...         return ""
    ...     @property
    ...     def site_runtime_dir(self):
    ...         return ""
    >>> # On machine A (Windows):
    >>> win_dirs = FakeDirs(r"C:\\Users\\alice\\AppData\\Local\\myapp", r"C:\\Users\\alice\\AppData\\Roaming\\myapp")
    >>> win_path = r"C:\\Users\\alice\\AppData\\Local\\myapp\\settings.toml"
    >>> encoded = encode_path(win_dirs, win_path)
    >>> encoded
    'config:settings.toml'
    >>> # On machine B (POSIX):
    >>> posix_dirs = FakeDirs("/home/alice/.config/myapp", "/home/alice/.local/share/myapp")
    >>> decoded = decode_path(posix_dirs, encoded)
    >>> str(decoded).replace("\\\\", "/")
    '/home/alice/.config/myapp/settings.toml'

The same encoded string ``config:settings.toml`` resolves to the correct local path on each machine.

"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .api import PlatformDirsABC

# Roles that can be encoded
_ROLES: Final[frozenset[str]] = frozenset({"config", "data"})

# Separator between role and segments in the encoded form
_ROLE_SEP: Final[str] = ":"
# Segment separator (always POSIX style for portability)
_SEG_SEP: Final[str] = "/"
# Marker for external (non-portable) paths
_EXTERNAL_MARKER: Final[str] = "external:"


class LogicalPathError(ValueError):
    """Raised when a logical path cannot be encoded or decoded."""


def _role_root(dirs: PlatformDirsABC, role: str) -> str:
    """Return the root directory for a role."""
    if role == "config":
        return dirs.user_config_dir
    if role == "data":
        return dirs.user_data_dir
    msg = f"unknown role: {role!r}"
    raise LogicalPathError(msg)


def _split_path(path: str) -> tuple[str, ...]:
    """Split a path into components, handling both POSIX and Windows separators.

    Handles Windows drive letters (e.g. ``C:``) and both separator styles. Empty components are dropped.

    """
    # Handle Windows drive
    drive, tail = os.path.splitdrive(path)
    if drive:
        # Drive is a component like "C:"
        parts = [drive.rstrip(":"), *tail.replace("\\", "/").strip("/").split("/")]
        return tuple(p for p in parts if p)
    # Normalize separators to POSIX
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return ()
    return tuple(p for p in normalized.split("/") if p)


def _is_within(path: str, root: str) -> bool:
    """Check if ``path`` is within ``root`` by complete path components, not string startswith.

    This prevents ``/a`` from matching ``/abc`` (a vs ab problem). On Windows, comparison is case-insensitive.

    """
    path_parts = _split_path(path)
    root_parts = _split_path(root)
    if len(path_parts) < len(root_parts):
        return False
    if os.name == "nt":
        # Case-insensitive comparison on Windows
        path_parts = tuple(p.lower() for p in path_parts)
        root_parts = tuple(p.lower() for p in root_parts)
    return path_parts[: len(root_parts)] == root_parts


def _relative_segments(path: str, root: str) -> tuple[str, ...]:
    """Return the relative segments of ``path`` from ``root``."""
    path_parts = _split_path(path)
    root_parts = _split_path(root)
    return path_parts[len(root_parts) :]


def _validate_segments(segments: tuple[str, ...]) -> None:
    """Validate that segments don't contain escape attempts or hidden separators.

    Rejects ``..`` (escape), ``.`` (no-op), hidden separators (``/``, ``\\``), and null bytes.

    """
    for seg in segments:
        if seg == "..":
            msg = "path segment '..' is not allowed (escape attempt)"
            raise LogicalPathError(msg)
        if seg == ".":
            msg = "path segment '.' is not allowed"
            raise LogicalPathError(msg)
        if "/" in seg or "\\" in seg:
            msg = f"path segment contains a hidden separator: {seg!r}"
            raise LogicalPathError(msg)
        if "\x00" in seg:
            msg = "path segment contains a null byte"
            raise LogicalPathError(msg)


def _normalize_root(root: str) -> str:
    """Normalize a root path for comparison (lowercase on Windows)."""
    if os.name == "nt":
        return root.lower()
    return root


def encode_path(dirs: PlatformDirsABC, path: str | os.PathLike[str]) -> str:
    """Encode a local path as a role + segment sequence.

    The path is matched against the config and data roots using complete path components (not string startswith). If
    multiple roots match, the longest one wins. If the same root is aliased by multiple roles, the ambiguity is
    rejected.

    :param dirs: PlatformDirs instance providing the roots.
    :param path: Local path to encode (absolute or relative).
    :returns: Encoded string like ``config:app/settings.toml``. External paths return ``external:<abspath>``.
    :raises LogicalPathError: if the path is ambiguous or contains unsafe segments.

    """
    path_str = os.fspath(path)
    # Resolve to absolute if relative.
    # On Windows, POSIX-style absolute paths (starting with /) are treated as relative by os.path.abspath,
    # so we keep them as-is to support cross-machine portability.
    if not Path(path_str).is_absolute() and not path_str.startswith("/"):
        path_str = os.path.abspath(path_str)  # ruff:ignore[os-path-abspath]

    # Find all roots that contain this path (by complete path components)
    matches: list[tuple[str, str]] = []
    for role in _ROLES:
        root = _role_root(dirs, role)
        if _is_within(path_str, root):
            matches.append((role, root))

    if not matches:
        # External path - not portable
        return f"{_EXTERNAL_MARKER}{path_str}"

    if len(matches) > 1:
        # Check if they're actually the same root (alias)
        normalized_roots = {_normalize_root(root) for _, root in matches}
        if len(normalized_roots) == 1:
            # Same root aliased by multiple roles - ambiguous
            roles = ", ".join(role for role, _ in matches)
            msg = f"path is under an aliased root shared by roles {roles}; cannot determine role"
            raise LogicalPathError(msg)
        # Different roots both contain the path - pick the longest
        matches.sort(key=lambda m: len(_split_path(m[1])), reverse=True)

    role, root = matches[0]
    segments = _relative_segments(path_str, root)
    _validate_segments(segments)

    return f"{role}{_ROLE_SEP}{_SEG_SEP.join(segments)}"


def decode_path(dirs: PlatformDirsABC, encoded: str) -> Path:
    """Decode a role + segment sequence to a local path.

    The role root is looked up from the current machine's PlatformDirs instance, and the segments are joined to it.
    External paths (``external:<abspath>``) are returned as-is.

    :param dirs: PlatformDirs instance providing the roots.
    :param encoded: Encoded string like ``config:app/settings.toml``.
    :returns: Decoded local path.
    :raises LogicalPathError: if the encoded form is invalid or contains unsafe segments.

    """
    if encoded.startswith(_EXTERNAL_MARKER):
        # External path - not portable, return as-is
        path_str = encoded[len(_EXTERNAL_MARKER) :]
        return Path(path_str)

    if _ROLE_SEP not in encoded:
        msg = f"encoded path must contain a role separator {_ROLE_SEP!r}, got {encoded!r}"
        raise LogicalPathError(msg)

    role, _, seg_str = encoded.partition(_ROLE_SEP)
    if role not in _ROLES:
        msg = f"unknown role: {role!r}"
        raise LogicalPathError(msg)

    segments = tuple(seg_str.split(_SEG_SEP)) if seg_str else ()
    _validate_segments(segments)

    root = _role_root(dirs, role)
    root_path = Path(root)
    if segments:
        return root_path.joinpath(*segments)
    return root_path


__all__ = [
    "LogicalPathError",
    "decode_path",
    "encode_path",
]
