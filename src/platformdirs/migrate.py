"""Config directory migration executor built on top of :class:`PlatformDirs`.

When an application is renamed, its per-user config directory (as resolved by
:class:`platformdirs.PlatformDirs`) usually changes too.  This module provides
a small, crash-safe executor that migrates the old config directory to the new
one in explicit, logged phases:

1. **plan**    - walk the source tree, refuse symlinks, record a digest for
  every regular file, and present the old -> new directory plan.
2. **confirm** - the caller (or an interactive prompt) must approve the plan.
3. **stage**   - copy the files into a temporary *staging* directory placed
  next to the target (a sibling, so the final publish is a same-filesystem
  atomic rename).
4. **verify**  - recompute the digest of the staged tree and compare it with
  the plan; publish only happens when everything matches.
5. **publish** - atomically rename the staging directory to the target.

Every phase appends a record to a *versioned* JSON-lines migration log whose
path identity is validated before use.  If the process is interrupted, a
later run can tell apart:

* a leftover **staging** directory (unpublished temporary content of the
  interrupted run -> cleaned up), and
* an already **published** target (recognised by its digest summary -> never
  overwritten again).

Only regular config files are migrated by default; cache directories are
never touched.  The source directory is preserved until the publish has been
confirmed in the log, and a failed run cleans up only its own temporary
files - an existing target is never deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from platformdirs import PlatformDirs

__all__ = [
    "LOG_SCHEMA_VERSION",
    "CopyFailed",
    "FileEntry",
    "LogPathError",
    "MigrationError",
    "MigrationExecutor",
    "MigrationPlan",
    "MigrationResult",
    "PlanError",
    "StagingError",
    "SymlinkRefused",
    "TargetConflict",
    "digest_of_tree",
    "validate_log_path",
]

#: Schema version written into every log record and encoded in the log name.
LOG_SCHEMA_VERSION = 1

#: Fixed log file name; the version is part of the identity of the log.
LOG_FILE_NAME = f"migration.v{LOG_SCHEMA_VERSION}.jsonl"

#: Suffix of the deterministic staging directory placed next to the target.
STAGING_SUFFIX = ".migrating"

# Phase names recorded in the migration log.
PHASE_PLAN = "plan"
PHASE_CONFIRM = "confirm"
PHASE_STAGE = "stage"
PHASE_VERIFY = "verify"
PHASE_PUBLISH = "publish"
PHASE_RECOVER = "recover"
PHASE_ABORT = "abort"
PHASE_DONE = "done"


class MigrationError(Exception):
    """Base class for all migration errors."""


class PlanError(MigrationError):
    """The migration plan could not be built."""


class SymlinkRefused(PlanError):
    """A symbolic link was found in the source tree; symlinks are refused."""


class TargetConflict(MigrationError):
    """The target already exists and does not match a published migration."""


class StagingError(MigrationError):
    """The staging directory failed identity checks."""


class CopyFailed(MigrationError):
    """Copying into the staging directory failed."""


class LogPathError(MigrationError):
    """The migration log path failed identity validation."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    """A single regular file planned for migration."""

    relpath: str
    size: int
    sha256: str


def _entries_of_tree(
    root: Path,
    exclude: Iterable[str | Path] = (),
) -> tuple[FileEntry, ...]:
    excluded = {os.path.normcase(os.path.abspath(path)) for path in exclude}
    entries: list[FileEntry] = []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        kept_dirnames: list[str] = []
        for name in sorted(dirnames):
            directory = current_path / name
            # Prune excluded directories (e.g. a cache dir nested inside the
            # config dir, as happens on Windows) from the walk entirely.
            if os.path.normcase(os.path.abspath(directory)) in excluded:
                continue
            if directory.is_symlink():
                msg = f"refusing symlinked directory in tree: {directory}"
                raise SymlinkRefused(msg)
            kept_dirnames.append(name)
        dirnames[:] = kept_dirnames
        for name in sorted(filenames):
            file_path = current_path / name
            if file_path.is_symlink():
                msg = f"refusing symlinked file in tree: {file_path}"
                raise SymlinkRefused(msg)
            if not file_path.is_file():
                msg = f"refusing non-regular file in tree: {file_path}"
                raise PlanError(msg)
            entries.append(
                FileEntry(
                    relpath=file_path.relative_to(root).as_posix(),
                    size=file_path.stat().st_size,
                    sha256=_sha256_file(file_path),
                )
            )
    entries.sort(key=lambda entry: entry.relpath)
    return tuple(entries)


def _digest_of_entries(entries: Iterable[FileEntry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def digest_of_tree(root: Path) -> str:
    """Return a deterministic summary digest of every regular file below *root*."""
    return _digest_of_entries(_entries_of_tree(root))


def validate_log_path(log_path: Path, log_dir: Path) -> Path:
    """Validate the identity of the migration log path.

    The log must be the expected versioned file directly inside *log_dir*,
    must not be reached through a symlink, and - if it already exists - must
    be a regular file.  Returns the validated path or raises
    :class:`LogPathError`.
    """
    log_path = Path(log_path)
    log_dir = Path(log_dir)
    if log_path.name != LOG_FILE_NAME:
        msg = f"unexpected migration log name {log_path.name!r}, expected {LOG_FILE_NAME!r}"
        raise LogPathError(msg)
    if log_dir.is_symlink() or log_path.is_symlink():
        msg = f"migration log path must not be a symlink: {log_path}"
        raise LogPathError(msg)
    if log_path.parent.absolute() != log_dir.absolute():
        msg = f"migration log {log_path} is not directly inside log dir {log_dir}"
        raise LogPathError(msg)
    if log_dir.exists() and not log_dir.is_dir():
        msg = f"migration log dir is not a directory: {log_dir}"
        raise LogPathError(msg)
    if log_path.exists() and not log_path.is_file():
        msg = f"migration log path is not a regular file: {log_path}"
        raise LogPathError(msg)
    return log_path


@dataclass(frozen=True)
class MigrationPlan:
    """The confirmed old -> new directory plan."""

    app_old: str
    app_new: str
    source: Path
    target: Path
    staging: Path
    log_dir: Path
    log_path: Path
    entries: tuple[FileEntry, ...]

    @property
    def digest(self) -> str:
        """Summary digest of the planned file set."""
        return _digest_of_entries(self.entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_old": self.app_old,
            "app_new": self.app_new,
            "source": str(self.source),
            "target": str(self.target),
            "staging": str(self.staging),
            "log_path": str(self.log_path),
            "file_count": len(self.entries),
            "total_bytes": sum(entry.size for entry in self.entries),
            "digest": self.digest,
            "files": [
                {"path": entry.relpath, "size": entry.size, "sha256": entry.sha256}
                for entry in self.entries
            ],
        }


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a migration run."""

    status: str  # "migrated" | "already-published" | "declined"
    plan: MigrationPlan
    digest: str
    log_path: Path
    details: dict[str, Any] = field(default_factory=dict)


def _default_copy(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst)


class MigrationExecutor:
    """Execute a crash-safe config directory migration.

    Parameters
    ----------
    source, target:
        Old and new config directories.  Use :meth:`from_platformdirs` to
        derive them from two :class:`PlatformDirs` instances.
    log_dir:
        Directory holding the versioned migration log.  Defaults to a
        ``.<target>.migration`` directory next to the target.
    copy_func:
        File copy callable (injectable for tests); defaults to
        :func:`shutil.copy2`.
    remove_source:
        Remove the source directory only *after* the publish has been
        confirmed in the log.  Defaults to ``False`` (source preserved).
    exclude_dirs:
        Directories pruned from the plan (e.g. the cache directory, which
        may be nested inside the config directory on some platforms).
    """

    def __init__(
        self,
        source: str | Path,
        target: str | Path,
        *,
        app_old: str = "",
        app_new: str = "",
        log_dir: str | Path | None = None,
        copy_func: Callable[[Path, Path], None] | None = None,
        remove_source: bool = False,
        exclude_dirs: Iterable[str | Path] = (),
    ) -> None:
        self.source = Path(source)
        self.target = Path(target)
        self.app_old = app_old
        self.app_new = app_new
        self.log_dir = (
            Path(log_dir)
            if log_dir is not None
            else self.target.parent / f".{self.target.name}.migration"
        )
        self.copy_func = copy_func or _default_copy
        self.remove_source = remove_source
        self.exclude_dirs = tuple(Path(path) for path in exclude_dirs)

    @classmethod
    def from_platformdirs(
        cls,
        old: PlatformDirs,
        new: PlatformDirs,
        **kwargs: Any,
    ) -> MigrationExecutor:
        """Build an executor migrating the old user config dir to the new one.

        The cache directory (``user_cache_dir``) is deliberately excluded
        from the plan - including when it is nested inside the config
        directory, as happens on Windows; only regular config files are
        migrated.
        """
        kwargs.setdefault("app_old", str(old.appname or ""))
        kwargs.setdefault("app_new", str(new.appname or ""))
        kwargs.setdefault("exclude_dirs", [Path(old.user_cache_path)])
        return cls(Path(old.user_config_path), Path(new.user_config_path), **kwargs)

    # -- plan -----------------------------------------------------------------

    def build_plan(self) -> MigrationPlan:
        """Generate the old -> new directory plan (phase ``plan``)."""
        if not self.source.is_dir():
            msg = f"source config directory does not exist: {self.source}"
            raise PlanError(msg)
        if self.source.is_symlink():
            msg = f"source config directory must not be a symlink: {self.source}"
            raise SymlinkRefused(msg)
        staging = self._staging_path()
        log_path = validate_log_path(self.log_dir / LOG_FILE_NAME, self.log_dir)
        entries = _entries_of_tree(self.source, exclude=self.exclude_dirs)
        return MigrationPlan(
            app_old=self.app_old,
            app_new=self.app_new,
            source=self.source,
            target=self.target,
            staging=staging,
            log_dir=self.log_dir,
            log_path=log_path,
            entries=entries,
        )

    def _staging_path(self) -> Path:
        staging = self.target.parent / f"{self.target.name}{STAGING_SUFFIX}"
        # Identity check: the staging dir must be a direct sibling of the
        # target so the publish rename stays on the same filesystem.
        if staging.parent.absolute() != self.target.parent.absolute():
            msg = f"staging dir {staging} is not a sibling of target {self.target}"
            raise StagingError(msg)
        if staging.is_symlink():
            msg = f"staging path must not be a symlink: {staging}"
            raise StagingError(msg)
        return staging

    # -- logging --------------------------------------------------------------

    def _append_log(self, plan: MigrationPlan, phase: str, **details: Any) -> None:
        record = {
            "version": LOG_SCHEMA_VERSION,
            "phase": phase,
            "time": datetime.now(tz=timezone.utc).isoformat(),
            "app_old": plan.app_old,
            "app_new": plan.app_new,
            "digest": plan.digest,
            "details": details,
        }
        plan.log_dir.mkdir(parents=True, exist_ok=True)
        validated = validate_log_path(plan.log_path, plan.log_dir)
        with validated.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    # -- phases ---------------------------------------------------------------

    def migrate(
        self,
        confirm: Callable[[MigrationPlan], bool] | None = None,
    ) -> MigrationResult:
        """Run the migration through all phases and return the result."""
        plan = self.build_plan()

        # Recovery: distinguish a previously published target from a
        # conflict, and clean up unpublished staging content left behind by
        # an interrupted run.
        recovered = self._recover(plan)
        if recovered is not None:
            return recovered

        self._append_log(plan, PHASE_PLAN, plan_summary=plan.as_dict())

        if confirm is not None and not confirm(plan):
            self._append_log(plan, PHASE_CONFIRM, accepted=False)
            return MigrationResult("declined", plan, plan.digest, plan.log_path)
        self._append_log(plan, PHASE_CONFIRM, accepted=True)

        try:
            self._stage(plan)
            self._verify(plan)
        except Exception as exc:
            # Failure before publish: clean up only the temporary files of
            # this run; the source and any pre-existing target stay
            # untouched.
            self._cleanup_staging(plan)
            self._append_log(plan, PHASE_ABORT, error=repr(exc))
            raise

        # Publish: atomic same-filesystem rename.  The log record is written
        # immediately afterwards; a crash in between is handled by _recover.
        os.rename(plan.staging, plan.target)
        self._append_log(plan, PHASE_PUBLISH, target=str(plan.target))

        if self.remove_source:
            shutil.rmtree(self.source)
            self._append_log(plan, PHASE_DONE, source_removed=True)
        else:
            self._append_log(plan, PHASE_DONE, source_removed=False)
        return MigrationResult("migrated", plan, plan.digest, plan.log_path)

    def _recover(self, plan: MigrationPlan) -> MigrationResult | None:
        # Leftover staging directory: unpublished temporary content of an
        # interrupted run.  Remove it (never the target) and continue.
        if plan.staging.exists():
            self._cleanup_staging(plan)
            self._append_log(plan, PHASE_RECOVER, action="removed-stale-staging")

        if self.target.exists():
            target_digest = digest_of_tree(self.target)
            if target_digest == plan.digest:
                # The target matches the planned digest summary: a previous
                # run published it (e.g. crashed before writing the publish
                # log record).  Recognise it instead of overwriting.
                self._append_log(
                    plan,
                    PHASE_RECOVER,
                    action="already-published",
                    target_digest=target_digest,
                )
                return MigrationResult(
                    "already-published",
                    plan,
                    target_digest,
                    plan.log_path,
                    {"target_digest": target_digest},
                )
            msg = (
                f"target already exists with unexpected content: {self.target} "
                f"(digest {target_digest}, expected {plan.digest})"
            )
            raise TargetConflict(msg)
        return None

    def _stage(self, plan: MigrationPlan) -> None:
        plan.staging.mkdir(parents=True, exist_ok=False)
        copied = 0
        try:
            for entry in plan.entries:
                destination = plan.staging / entry.relpath
                destination.parent.mkdir(parents=True, exist_ok=True)
                self.copy_func(plan.source / entry.relpath, destination)
                copied += 1
        except Exception as exc:
            msg = f"failed to stage file {copied + 1}/{len(plan.entries)}: {exc}"
            raise CopyFailed(msg) from exc
        self._append_log(plan, PHASE_STAGE, files_copied=copied)

    def _verify(self, plan: MigrationPlan) -> None:
        staged_digest = digest_of_tree(plan.staging)
        if staged_digest != plan.digest:
            msg = (
                f"staged tree digest {staged_digest} does not match "
                f"planned digest {plan.digest}"
            )
            raise MigrationError(msg)
        self._append_log(plan, PHASE_VERIFY, staged_digest=staged_digest)

    def _cleanup_staging(self, plan: MigrationPlan) -> None:
        staging = plan.staging
        # Only ever remove the exact deterministic staging directory, and
        # never follow a symlink or touch the published target.
        if staging.name != f"{self.target.name}{STAGING_SUFFIX}":
            msg = f"refusing to clean unexpected staging path: {staging}"
            raise StagingError(msg)
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)


def _demo() -> None:  # pragma: no cover - small interactive demonstration
    import tempfile

    from platformdirs import PlatformDirs

    with tempfile.TemporaryDirectory(prefix="platformdirs-migrate-demo-") as tmp:
        # In real usage the directories come straight from PlatformDirs:
        old = PlatformDirs("OldApp", "DemoCo")
        new = PlatformDirs("NewApp", "DemoCo")
        print("PlatformDirs would resolve:")
        print(f"  old config: {old.user_config_path}")
        print(f"  new config: {new.user_config_path}")
        print("Running the demo inside a sandbox instead.\n")

        source = Path(tmp) / "OldApp"
        target = Path(tmp) / "NewApp"
        source.mkdir()
        for index in range(3):
            (source / f"settings-{index}.ini").write_text(f"[demo]\nvalue={index}\n")

        executor = MigrationExecutor(source, target, app_old="OldApp", app_new="NewApp")
        plan = executor.build_plan()
        print("Plan: old -> new")
        print(f"  {plan.source} -> {plan.target}")
        print(f"  staging: {plan.staging}")
        print(f"  log:     {plan.log_path}")
        print(f"  files:   {[entry.relpath for entry in plan.entries]}")
        print(f"  digest:  {plan.digest}")

        result = executor.migrate(confirm=lambda plan_: True)
        print(f"Result: {result.status} (digest {result.digest})")

        # Recovery demo: a second run recognises the published target.
        again = MigrationExecutor(source, target).migrate()
        print(f"Re-run: {again.status}")

        print("Migration log:")
        print(result.log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    _demo()
