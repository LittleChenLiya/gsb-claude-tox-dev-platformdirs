"""Encode local config/data paths as portable logical references and rebind them on another machine.

A *portable reference* describes a path as a **role** (``config``, ``data``, ``cache``, ``state`` or ``log``) plus a
sequence of **path segments** relative to that role's root. Any machine can then rebind the reference to its own
:class:`~platformdirs.api.PlatformDirsABC` roots:

.. code-block:: python

    # Machine A (Windows) - encode a declared local path:
    >>> from platformdirs import PlatformDirs
    >>> from platformdirs.portable import PortableCodec
    >>> codec_a = PortableCodec(PlatformDirs("Acme", "AcmeCorp"))
    >>> ref = codec_a.encode(codec_a.root_for("cache") / "thumbs" / "a.png")
    >>> payload = ref.to_payload()
    >>> payload
    'cache:thumbs/a.png'

    # Machine B (Linux) - rebind the same payload to its own roots:
    >>> codec_b = PortableCodec(PlatformDirs("Acme", "AcmeCorp"))
    >>> codec_b.decode(payload)  # e.g. PosixPath('/home/tux/.cache/AcmeCorp/Acme/thumbs/a.png')
    ...

Guarantees:

* Roots are matched by **longest full segment-wise prefix** (never by string ``startswith``), so sibling roots such as
  ``.../a`` and ``.../ab`` are never confused, and a nested root (e.g. ``cache`` under ``data``) wins over its parent.
* Decoding rejects segments that could escape the role root (``..``, ``.``, empty segments) or smuggle hidden
  separators/anchors (``/``, backslash, ``:``, NUL).
* If several roles alias the *same* root (e.g. Windows ``config`` == ``data``) and a path matches that aliased root,
  encoding raises :class:`AmbiguousRootError` instead of guessing.
* Paths outside every known root are *not portable*: :meth:`PortableCodec.encode` raises :class:`PathNotPortableError`
  and :meth:`PortableCodec.try_encode` returns an :class:`ExternalRef` marker.
* The codec is pure path arithmetic: it **never creates directories** and **never reads, moves or copies files**
  (cache files included). Only the caller decides what to transfer.

"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

__all__ = [
    "AmbiguousRootError",
    "ExternalRef",
    "InvalidPortableRefError",
    "PathNotPortableError",
    "PortableCodec",
    "PortablePathError",
    "PortableRef",
]

#: Roles supported by default, mapped to the ``PlatformDirs`` attribute prefix (``user_<role>_dir``).
DEFAULT_ROLES: tuple[str, ...] = ("config", "data", "cache", "state", "log")

_ROLE_RE = re.compile(r"[a-z][a-z0-9_]*")
#: Characters that may never appear inside a decoded segment: separators, drive/anchor markers and NUL.
_FORBIDDEN_SEGMENT_CHARS = frozenset({"/", chr(92), ":", chr(0)}) | ({os.sep, os.altsep} - {None})


class PortablePathError(Exception):
    """Base class for all portable-path encoding/decoding failures."""


class PathNotPortableError(PortablePathError):
    """The path does not live under any known role root, so it cannot be ported to another machine."""


class AmbiguousRootError(PortablePathError):
    """Several roles alias the same longest matching root, so the owning role cannot be determined."""


class InvalidPortableRefError(PortablePathError, ValueError):
    """A portable reference or payload is malformed or its segments could escape the role root."""


def _validate_role(role: str) -> None:
    if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
        msg = f"invalid role name: {role!r}"
        raise InvalidPortableRefError(msg)


def _validate_segments(segments: Iterable[str]) -> tuple[str, ...]:
    result = tuple(segments)
    for segment in result:
        if not isinstance(segment, str) or not segment:
            msg = f"invalid empty path segment in {result!r}"
            raise InvalidPortableRefError(msg)
        if segment in {".", ".."}:
            msg = f"path segment {segment!r} may escape the role root"
            raise InvalidPortableRefError(msg)
        bad = _FORBIDDEN_SEGMENT_CHARS.intersection(segment)
        if bad:
            msg = f"path segment {segment!r} contains hidden separator/anchor character(s) {sorted(bad)!r}"
            raise InvalidPortableRefError(msg)
    return result


def _key(path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return a normalized, case-folded segment tuple for *path* without touching the filesystem."""
    normalized = os.path.normcase(os.path.normpath(str(path)))
    return PurePath(normalized).parts


def _is_prefix(prefix: tuple[str, ...], whole: tuple[str, ...]) -> bool:
    return len(prefix) <= len(whole) and whole[: len(prefix)] == prefix


@dataclass(frozen=True)
class PortableRef:
    """A portable logical path: a *role* plus root-relative *segments*. Always portable."""

    role: str
    segments: tuple[str, ...] = ()
    portable: ClassVar[bool] = True

    def __post_init__(self) -> None:
        """Validate the role and segments at construction time."""
        _validate_role(self.role)
        object.__setattr__(self, "segments", _validate_segments(self.segments))

    def to_payload(self) -> str:
        """Serialize to a machine-independent string, e.g. ``config:app/settings.toml``."""
        return f"{self.role}:{'/'.join(self.segments)}"

    @classmethod
    def from_payload(cls, payload: str) -> PortableRef:
        """Parse a payload produced by :meth:`to_payload`, validating every segment."""
        if not isinstance(payload, str) or ":" not in payload:
            msg = f"invalid portable payload: {payload!r}"
            raise InvalidPortableRefError(msg)
        role, _, rest = payload.partition(":")
        segments = tuple(rest.split("/")) if rest else ()
        return cls(role=role, segments=segments)

    def __str__(self) -> str:
        """Return the payload form of this reference."""
        return self.to_payload()


@dataclass(frozen=True)
class ExternalRef:
    """Marker for a path outside every role root: it is tied to this machine and cannot be ported."""

    path: str
    portable: ClassVar[bool] = False
    reason: str = field(default="path is outside every known role root")

    def __str__(self) -> str:
        """Return a human-readable marker for this non-portable path."""
        return f"external:{self.path}"


class PortableCodec:
    """Encode local paths to :class:`PortableRef` and decode references back against this machine's roots."""

    def __init__(self, dirs: object, *, roles: Iterable[str] = DEFAULT_ROLES) -> None:
        """Bind the codec to the ``user_<role>_dir`` roots exposed by *dirs* (e.g. a ``PlatformDirs`` instance)."""
        self._dirs = dirs
        self._roots: dict[str, Path] = {}
        for role in roles:
            _validate_role(role)
            self._roots[role] = self._read_root(dirs, role)
        # Longest root first so nested roots (e.g. cache under data) win; ties are detected as ambiguity.
        self._match_order: tuple[tuple[tuple[str, ...], str], ...] = tuple(
            sorted(((_key(root), role) for role, root in self._roots.items()), key=lambda item: -len(item[0]))
        )

    @staticmethod
    def _read_root(dirs: object, role: str) -> Path:
        # Prefer the ``*_dir`` string properties: unlike ``*_path`` they never create directories as a side effect.
        for suffix in ("dir", "path"):
            value = getattr(dirs, f"user_{role}_{suffix}", None)
            if value is not None:
                return Path(value)
        msg = f"{dirs!r} exposes no user_{role}_dir/user_{role}_path root"
        raise AttributeError(msg)

    @property
    def roles(self) -> tuple[str, ...]:
        """Roles this codec can encode to and decode from."""
        return tuple(self._roots)

    def root_for(self, role: str) -> Path:
        """Return this machine's root directory for *role*."""
        try:
            return self._roots[role]
        except KeyError:
            msg = f"unknown role {role!r}; known roles: {sorted(self._roots)}"
            raise InvalidPortableRefError(msg) from None

    def encode(self, path: str | os.PathLike[str]) -> PortableRef:
        """Encode a local absolute *path* as a :class:`PortableRef`.

        :raises PathNotPortableError: if *path* is outside every role root.
        :raises AmbiguousRootError: if the longest matching root is aliased by more than one role.
        """
        parts = _key(path)
        best_len = -1
        best_roles: list[str] = []
        for root_key, role in self._match_order:
            if len(root_key) < best_len:
                break  # sorted by descending length: nothing shorter can tie or win
            if _is_prefix(root_key, parts):
                if len(root_key) > best_len:
                    best_len, best_roles = len(root_key), [role]
                else:
                    best_roles.append(role)
        if best_len < 0:
            msg = f"path {str(path)!r} is outside every known role root and cannot be ported"
            raise PathNotPortableError(msg)
        if len(best_roles) > 1:
            msg = f"path {str(path)!r} matches aliased roots for roles {sorted(best_roles)}; refusing to guess"
            raise AmbiguousRootError(msg)
        (role,) = best_roles
        return PortableRef(role=role, segments=parts[best_len:])

    def try_encode(self, path: str | os.PathLike[str]) -> PortableRef | ExternalRef:
        """Like :meth:`encode`, but return an :class:`ExternalRef` marker instead of raising for outside paths."""
        try:
            return self.encode(path)
        except PathNotPortableError as exc:
            return ExternalRef(path=str(path), reason=str(exc))

    def decode(self, ref: PortableRef | str) -> Path:
        """Rebind a :class:`PortableRef` or payload string to this machine's root for its role.

        Pure path arithmetic: no directory is created and no file is read, moved or copied.
        """
        if isinstance(ref, str):
            ref = PortableRef.from_payload(ref)
        if not isinstance(ref, PortableRef):
            msg = f"cannot decode object of type {type(ref).__name__}"
            raise InvalidPortableRefError(msg)
        root = self.root_for(ref.role)
        segments = _validate_segments(ref.segments)  # re-validate: never trust a reference across the wire
        result = root.joinpath(*segments)
        # Defense in depth: the rebound path must still live inside the role root, segment for segment.
        root_key, result_key = _key(root), _key(result)
        if not _is_prefix(root_key, result_key) or result_key[len(root_key) :] != tuple(
            os.path.normcase(s) for s in segments
        ):
            msg = f"decoded path {str(result)!r} escapes role root {str(root)!r}"
            raise InvalidPortableRefError(msg)
        return result

    def iter_roots(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(role, root)`` pairs known to this codec."""
        yield from self._roots.items()
