"""Generation manager: seal-before-switch cache generations with lease-protected reclamation.

A :class:`GenerationManager` manages a cache root containing immutable generation
directories. Publishing stages a new generation, seals it, and only then atomically
switches ``current`` to it. Readers take a :class:`Lease` on the generation they read;
a leased generation cannot be reclaimed until every lease is returned. Only directories
the manager itself marked as retired are ever deleted, and deletion never follows
symlinks.

"""

from __future__ import annotations

import os
import secrets
import threading
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

__all__ = [
    "GenerationError",
    "GenerationManager",
    "Lease",
    "LeaseError",
    "NoCurrentError",
    "OwnershipError",
]

SEALED_MARKER = "SEALED"  #: Marker written when a generation is sealed.
RETIRED_MARKER = "RETIRED"  #: Marker written when a generation is retired.
OWNER_MARKER = "OWNER"  #: Marker holding the owning manager token.
CURRENT_MARKER = "current"  #: Pointer to the current generation.
GENERATIONS_DIR = "generations"  #: Directory holding generation directories.
LEASES_DIR = "leases"  #: Directory holding lease marker files.
STAGING_PREFIX = ".tmp-"  #: Prefix for in-progress staging directories.


class GenerationError(Exception):
    """Base class for generation manager errors."""


class OwnershipError(GenerationError):
    """Refusing to take over a root owned by another, live owner."""


class LeaseError(GenerationError):
    """Operation refused because one or more leases are still active."""


class NoCurrentError(GenerationError):
    """No current generation exists yet."""


class Lease:
    """A read lease on a specific generation.

    While a lease is held, the leased generation cannot be reclaimed by
    :meth:`GenerationManager.gc`. A lease is returned with :meth:`release` (or by
    using it as a context manager).

    """

    __slots__ = ("_gen_id", "_lease_id", "_manager", "_released")

    def __init__(self, manager: GenerationManager, lease_id: str, gen_id: str) -> None:
        """Initialize a lease for ``gen_id``."""
        self._manager = manager
        self._lease_id = lease_id
        self._gen_id = gen_id
        self._released = False

    @property
    def generation(self) -> Path:
        """The directory containing the leased generation."""
        return self._manager.generations_dir / self._gen_id

    @property
    def gen_id(self) -> str:
        """The id of the leased generation."""
        return self._gen_id

    def release(self) -> None:
        """Return the lease, allowing the generation to be reclaimed.

        Idempotent: releasing an already returned lease is a no-op.

        """
        if self._released:
            return
        self._released = True
        self._manager._release_lease(self._lease_id)  # ruff: ignore[private-member-access]

    def __enter__(self) -> Lease:  # ruff: ignore[non-self-return-type]
        """Enter the context, returning the lease."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Exit the context, releasing the lease."""
        self.release()


class GenerationManager:
    """Manage sealed, atomically switched, lease-protected cache generations in an explicit root.

    The root layout is::

        <root>/
            OWNER            # owner token (pid:token) of the managing process
            current          # pointer to the current generation (symlink or marker file)
            generations/     # one subdirectory per generation, plus staging dirs
                <gen-id>/    # a sealed generation (may carry a RETIRED marker)
            leases/          # one marker file per active lease

    Lifecycle: :meth:`publish` stages a new generation, seals it, retires the previous
    current generation, and only then atomically switches ``current`` to the new one.
    Retired generations are reclaimed by :meth:`gc` only once no :class:`Lease`
    references them. User directories that were never marked by the manager are kept
    forever, and deletion never follows symlinks.

    :param root: the explicit cache root to manage. It is created if it does not exist.
    :raises OwnershipError: if another live owner already manages ``root``.

    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        """Open (or create) the cache ``root`` and claim ownership of it."""
        self.root = Path(root).resolve()
        self.generations_dir = self.root / GENERATIONS_DIR
        self.leases_dir = self.root / LEASES_DIR
        self._lock = threading.RLock()
        self._staging: set[Path] = set()
        self._closed = False
        self._owner_token = f"{os.getpid()}:{secrets.token_hex(8)}"
        self.root.mkdir(parents=True, exist_ok=True)
        self._acquire_ownership()
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        self.leases_dir.mkdir(parents=True, exist_ok=True)
        self._symlink_current = self._detect_symlink_mode()

    def _acquire_ownership(self) -> None:
        owner = self.root / OWNER_MARKER
        pid: int | None = None
        if owner.exists() or owner.is_symlink():
            try:
                content = owner.read_text(encoding="utf-8").strip()
            except OSError:
                content = ""
            pid = _parse_owner_pid(content)
            if pid is not None and _pid_alive(pid):
                msg = f"refusing to take over {self.root}: another root owner (pid {pid}) is active"
                raise OwnershipError(msg)
        self._safe_write_text(owner, self._owner_token)

    def _detect_symlink_mode(self) -> bool:
        # Symlinks are preferred (current is a real symlink), but creating them needs a
        # privilege on Windows that may be absent; fall back to a marker file then.
        probe = self.root / ".symlink-probe"
        try:
            probe.symlink_to("probe-target")
        except OSError:
            return False
        probe.unlink()
        return True

    def _ensure_open(self) -> None:
        if self._closed:
            msg = f"manager for {self.root} is closed"
            raise GenerationError(msg)

    def publish(self, populate: Callable[[Path], None] | None = None) -> Path:
        """Publish a new generation.

        The new generation is staged, sealed, and only then made current. If anything
        fails before the switch, the staging directory is reclaimed and ``current`` is
        left untouched.

        :param populate: optional callback invoked with the staging directory so the
            caller can write the generation contents before sealing.
        :returns: the path to the new current generation directory.

        """
        self._ensure_open()
        with self._lock:
            gen_id = secrets.token_hex(8)
            tmp = self.generations_dir / f"{STAGING_PREFIX}{gen_id}"
            final = self.generations_dir / gen_id
            tmp.mkdir(parents=True, exist_ok=False)
            self._staging.add(tmp)
            try:
                if populate is not None:
                    populate(tmp)
                self._seal(tmp)
                self._commit(tmp, final, gen_id)
            except BaseException:
                # Reclaim this attempt's directory only if the switch did not happen;
                # never touch a generation that is already current.
                self._staging.discard(tmp)
                if self._current_gen_id() != gen_id:
                    target = final if final.exists() or final.is_symlink() else tmp
                    self._rmtree_no_follow(target)
                raise
            return final

    def _commit(self, tmp: Path, final: Path, gen_id: str) -> None:
        tmp.replace(final)
        self._staging.discard(tmp)
        old_id = self._current_gen_id()
        if old_id is not None:
            self._retire(old_id)
        self._switch_current(gen_id)

    def _seal(self, gen_dir: Path) -> None:
        self._safe_write_text(gen_dir / SEALED_MARKER, f"{self._owner_token}\n{secrets.token_hex(4)}\n")

    def _retire(self, gen_id: str) -> None:
        gen_dir = self.generations_dir / gen_id
        if gen_dir.is_symlink() or not gen_dir.is_dir():
            # Not a real generation directory (e.g. a user entry); never touch it.
            return
        self._safe_write_text(gen_dir / RETIRED_MARKER, f"{self._owner_token}\n")

    def current(self) -> Path:
        """Return the path to the current (sealed) generation directory.

        :raises NoCurrentError: if no generation has been published yet.

        """
        self._ensure_open()
        with self._lock:
            gen_id = self._current_gen_id()
            if gen_id is None:
                msg = f"no current generation in {self.root}"
                raise NoCurrentError(msg)
            return self.generations_dir / gen_id

    def _current_gen_id(self) -> str | None:
        current = self.root / CURRENT_MARKER
        if current.is_symlink():
            target = current.readlink().replace("\\", "/").rstrip("/")
            name = target.rsplit("/", 1)[-1]
            return name or None
        if current.is_file():
            return current.read_text(encoding="utf-8").strip() or None
        return None

    def _switch_current(self, gen_id: str) -> None:
        current = self.root / CURRENT_MARKER
        if self._symlink_current:
            tmp = self.root / f".current-tmp-{gen_id}"
            try:
                tmp.symlink_to(f"{GENERATIONS_DIR}/{gen_id}")
                tmp.replace(current)
            except OSError:
                # Symlinks unsupported (or replace failed); clean up and use a marker file.
                with suppress(OSError):
                    tmp.unlink()
                self._symlink_current = False
            else:
                return
        tmp = self.root / f".current-tmp-{gen_id}"
        self._safe_write_text(tmp, gen_id)
        tmp.replace(current)

    def lease(self) -> Lease:
        """Take a read lease on the current generation.

        While the lease is active, the generation cannot be reclaimed by :meth:`gc`.

        :raises NoCurrentError: if no generation has been published yet.

        """
        self._ensure_open()
        with self._lock:
            gen_id = self._current_gen_id()
            if gen_id is None:
                msg = f"no current generation in {self.root}"
                raise NoCurrentError(msg)
            lease_id = secrets.token_hex(8)
            self._safe_write_text(self.leases_dir / lease_id, gen_id)
            return Lease(self, lease_id, gen_id)

    def _release_lease(self, lease_id: str) -> None:
        with self._lock, suppress(FileNotFoundError):
            (self.leases_dir / lease_id).unlink()

    def _leased_gen_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.leases_dir.exists():
            return ids
        for entry in self.leases_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                content = entry.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                ids.add(content)
        return ids

    def gc(self) -> list[Path]:
        """Reclaim retired generations that are no longer leased.

        Only directories marked as retired by this manager are deleted; user
        directories without a ``RETIRED`` marker are kept forever. Deletion never
        follows symlinks. Stale staging directories left by dead processes are also
        reclaimed.

        :returns: the paths that were deleted.

        """
        self._ensure_open()
        with self._lock:
            current_id = self._current_gen_id()
            leased = self._leased_gen_ids()
            deleted: list[Path] = []
            for entry in sorted(self.generations_dir.iterdir(), key=lambda p: p.name):
                name = entry.name
                if name.startswith(STAGING_PREFIX):
                    if entry in self._staging:
                        continue  # an in-progress staging dir of this manager
                    self._rmtree_no_follow(entry)
                    deleted.append(entry)
                    continue
                if entry.is_symlink() or not entry.is_dir():
                    continue  # a user entry; never touch
                if not (entry / RETIRED_MARKER).is_file():
                    continue  # unmarked user directory; keep forever
                if name == current_id:
                    continue  # the current generation is never reclaimed
                if name in leased:
                    continue  # still leased; wait for the lease to be returned
                self._rmtree_no_follow(entry)
                deleted.append(entry)
            return deleted

    def close(self) -> None:
        """Close the manager, releasing root ownership.

        Refuses to close while leases are still active: return every lease first.
        No files are deleted by closing.

        :raises LeaseError: if one or more leases are still active.

        """
        with self._lock:
            if self._closed:
                return
            active = self._active_leases()
            if active:
                msg = (
                    f"cannot close {self.root}: {len(active)} lease(s) still active "
                    f"({', '.join(active)}); return them before closing"
                )
                raise LeaseError(msg)
            with suppress(FileNotFoundError):
                (self.root / OWNER_MARKER).unlink()
            self._closed = True

    def _active_leases(self) -> list[str]:
        if not self.leases_dir.exists():
            return []
        return sorted(entry.name for entry in self.leases_dir.iterdir() if not entry.is_symlink() and entry.is_file())

    def __enter__(self) -> GenerationManager:  # ruff: ignore[non-self-return-type]
        """Enter the context, returning the manager."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Exit the context, closing the manager."""
        self.close()

    @staticmethod
    def _safe_write_text(path: Path, text: str) -> None:
        # Never follow a symlink when writing a marker.
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _rmtree_no_follow(path: str | os.PathLike[str]) -> None:
        # Recursively delete a directory without ever following symlinks: symlinks are
        # unlinked, never descended into, so a link to an outside directory is removed
        # as a link and its target is left untouched.
        path = Path(path)
        if path.is_symlink():
            path.unlink()
            return
        if not path.is_dir():
            path.unlink(missing_ok=True)
            return
        for entry in path.iterdir():
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                GenerationManager._rmtree_no_follow(entry)
            else:
                entry.unlink()
        path.rmdir()


def _parse_owner_pid(content: str) -> int | None:
    token = content.strip()
    if ":" in token:
        token = token.split(":", 1)[0]
    try:
        return int(token)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes  # ruff: ignore[import-outside-top-level]

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,  # ruff: ignore[boolean-positional-value-in-call]
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
