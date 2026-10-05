"""Generation manager for an explicit cache root.

A :class:`GenerationManager` manages immutable *generations* inside a
caller-supplied cache root directory:

* A new generation directory is only switched to ``current`` **after** it has
  been sealed.  A failed seal reclaims the half-built directory and leaves
  ``current`` untouched.
* Readers hold *leases* on a generation.  A retired generation with active
  leases is never cleaned up; it is reclaimed only once the last lease is
  returned.
* Only directories carrying this manager's own marker are ever deleted.
  Foreign/user directories in the root are preserved forever, and symlinks
  are never followed or removed.
* If the root is already owned by another manager owner, the manager refuses
  to take it over.
* Closing the manager while leases are still outstanding raises instead of
  silently deleting files.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "GenerationManager",
    "GenerationError",
    "OwnershipError",
    "LeaseActiveError",
    "Lease",
]

#: Marker placed in every generation directory created by this manager.
GEN_MARKER = ".gencache-gen"
#: Marker written once a generation has been sealed successfully.
SEAL_MARKER = ".gencache-sealed"
#: File in the root recording which generation is current.
CURRENT_FILE = "current"
#: File in the root recording the owner token of the managing instance.
OWNER_FILE = "owner"
#: Prefix of every generation directory name.
GEN_PREFIX = "gen-"


class GenerationError(RuntimeError):
    """Base error for generation manager failures."""


class OwnershipError(GenerationError):
    """Raised when the cache root is owned by another manager owner."""


class LeaseActiveError(GenerationError):
    """Raised when closing a manager that still has outstanding leases."""


@dataclass
class Lease:
    """A read lease on a generation.  Use as a context manager."""

    _manager: "GenerationManager"
    generation: str
    path: Path
    _returned: bool = False

    def release(self) -> None:
        """Return the lease, allowing the generation to be reclaimed."""
        if not self._returned:
            self._returned = True
            self._manager._release_lease(self)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class _Build:
    """Handle for a generation under construction."""

    def __init__(self, manager: "GenerationManager", name: str, path: Path) -> None:
        self._manager = manager
        self.name = name
        self.path = path
        self._done = False

    def seal(self) -> str:
        """Seal the generation and atomically switch ``current`` to it."""
        if self._done:
            raise GenerationError(f"generation {self.name} already sealed or aborted")
        self._done = True
        self._manager._seal(self)
        return self.name


class GenerationManager:
    """Manage sealed generations inside an explicit cache root."""

    def __init__(self, root: "str | os.PathLike[str]", owner: str) -> None:
        if not owner:
            raise ValueError("owner token must be a non-empty string")
        self.root = Path(root)
        self.owner = owner
        self._lock = threading.RLock()
        self._leases: "dict[str, int]" = {}
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True)
        self._claim_root()

    # -- ownership ---------------------------------------------------------

    def _claim_root(self) -> None:
        owner_file = self.root / OWNER_FILE
        if owner_file.exists() and not owner_file.is_symlink():
            existing = owner_file.read_text(encoding="utf-8").strip()
            if existing and existing != self.owner:
                raise OwnershipError(
                    f"cache root {self.root} is owned by {existing!r}; "
                    "refusing to take over"
                )
        else:
            self._atomic_write(owner_file, self.owner + "\n")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _current_name(self) -> "str | None":
        current = self.root / CURRENT_FILE
        if not current.exists() or current.is_symlink():
            return None
        name = current.read_text(encoding="utf-8").strip()
        return name or None

    def _gen_path(self, name: str) -> Path:
        return self.root / name

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        """True for symlinks and (on Windows) junctions / reparse points."""
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())

    def _is_our_gen(self, path: Path) -> bool:
        """True only for real, marked, non-symlink generation directories."""
        return (
            path.is_dir()
            and not self._is_link_like(path)
            and path.name.startswith(GEN_PREFIX)
            and (path / GEN_MARKER).is_file()
        )

    def _remove_tree_no_symlink(self, path: Path) -> None:
        """Remove a marked generation directory without following symlinks."""
        if self._is_link_like(path) or not path.exists():
            return
        if not self._is_our_gen(path):
            raise GenerationError(f"refusing to delete unmarked directory {path}")
        shutil.rmtree(path)  # shutil.rmtree never follows symlinks

    # -- publishing --------------------------------------------------------

    def _next_name(self) -> str:
        existing = [
            p.name
            for p in self.root.iterdir()
            if p.name.startswith(GEN_PREFIX) and p.name[len(GEN_PREFIX):].isdigit()
        ]
        nxt = max((int(n[len(GEN_PREFIX):]) for n in existing), default=0) + 1
        return f"{GEN_PREFIX}{nxt}"

    def begin(self) -> _Build:
        """Start building a new generation directory (not yet current)."""
        with self._lock:
            self._ensure_open()
            name = self._next_name()
            path = self._gen_path(name)
            path.mkdir()
            (path / GEN_MARKER).write_text(f"owner={self.owner}\n", encoding="utf-8")
            return _Build(self, name, path)

    @contextlib.contextmanager
    def build(self) -> "Iterator[_Build]":
        """Context manager: seal on success, reclaim the directory on failure."""
        build = self.begin()
        try:
            yield build
        except BaseException:
            # Failed build: reclaim this attempt's directory, keep current.
            build._done = True
            with self._lock:
                self._remove_tree_no_symlink(build.path)
            raise
        else:
            if not build._done:
                build.seal()

    def _seal(self, build: _Build) -> None:
        with self._lock:
            self._ensure_open()
            try:
                (build.path / SEAL_MARKER).write_text(
                    f"owner={self.owner}\n", encoding="utf-8"
                )
            except BaseException:
                # Seal failed: reclaim this attempt's directory, keep current.
                self._remove_tree_no_symlink(build.path)
                raise
            # Only after sealing do we switch current (atomic replace).
            self._atomic_write(self.root / CURRENT_FILE, build.name + "\n")
            self._reclaim_locked()

    # -- reading -----------------------------------------------------------

    def acquire(self) -> Lease:
        """Acquire a read lease on the current generation."""
        with self._lock:
            self._ensure_open()
            name = self._current_name()
            if name is None:
                raise GenerationError("no generation has been published yet")
            path = self._gen_path(name)
            self._leases[name] = self._leases.get(name, 0) + 1
            return Lease(self, name, path)

    def _release_lease(self, lease: Lease) -> None:
        with self._lock:
            remaining = self._leases.get(lease.generation, 0) - 1
            if remaining > 0:
                self._leases[lease.generation] = remaining
            else:
                self._leases.pop(lease.generation, None)
            self._reclaim_locked()

    def active_leases(self) -> int:
        with self._lock:
            return sum(self._leases.values())

    # -- reclamation -------------------------------------------------------

    def _reclaim_locked(self) -> None:
        current = self._current_name()
        for entry in sorted(self.root.iterdir()):
            if entry.name == current:
                continue
            if not self._is_our_gen(entry):
                continue  # never touch unmarked/user directories or symlinks
            if self._leases.get(entry.name, 0) > 0:
                continue  # leased generations must survive
            self._remove_tree_no_symlink(entry)

    def reclaim(self) -> None:
        """Public hook: reclaim retired, unleased, marked generations."""
        with self._lock:
            self._ensure_open()
            self._reclaim_locked()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise GenerationError("generation manager is closed")

    def close(self) -> None:
        """Close the manager.  Outstanding leases must be returned first."""
        with self._lock:
            outstanding = self.active_leases()
            if outstanding:
                raise LeaseActiveError(
                    f"{outstanding} lease(s) still active; return them before closing"
                )
            self._closed = True

    def __enter__(self) -> "GenerationManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
