"""Resumable migration executor for renaming an application's config directory.

When an application is renamed, the configuration directory determined by
:class:`platformdirs.PlatformDirs` changes too. This module provides a small,
resumable migration executor that:

#. builds and confirms an old-to-new directory plan,
#. copies only regular configuration files (cache directories and symbolic links
   are rejected) to a temporary directory next to the new target,
#. verifies the copy against the source,
#. publishes the target by atomically renaming the temporary directory into place,
   and
#. writes a versioned JSON migration log after every stage.

The migration is resumable: if the process is interrupted, re-running detects
whether the temporary directory is unpublished or the target is already
published (by comparing content digests) and continues accordingly without
overwriting a published target. The source directory is preserved until publish
is confirmed; on failure only this run's temporary files are removed and an
existing target is never deleted.

"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from platformdirs import PlatformDirs

if TYPE_CHECKING:
    from collections.abc import Callable

LOG_VERSION = 1
_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class MigrationError(Exception):
    """Base class for migration errors."""


class TargetExistsError(MigrationError):
    """The target directory already exists and does not match the source."""


class SymlinkRejectedError(MigrationError):
    """A symbolic link was encountered and rejected."""


class VerificationError(MigrationError):
    """A copied file failed verification against the source."""


class LogIdentityError(MigrationError):
    """The migration log's identity does not match the requested migration."""


@dataclass(frozen=True)
class FileEntry:
    """A single migrated file's relative path, size, and SHA-256 digest."""

    relpath: str
    size: int
    digest: str


@dataclass(frozen=True)
class MigrationPlan:
    """A planned migration: source and target directories plus the file manifest."""

    old_appname: str
    new_appname: str
    source_dir: Path
    target_dir: Path
    temp_dir: Path
    log_path: Path
    files: tuple[FileEntry, ...]
    source_digest: str
    run_id: str


@dataclass
class MigrationHooks:
    """Optional callbacks invoked between stages; raising simulates an interruption.

    Each hook is called with no arguments. If a hook raises an exception the
    migration is treated as interrupted: this run's temporary files are removed
    (if publish has not happened yet) and the exception propagates. An existing
    target is never deleted.

    """

    before_copy: Callable[[], None] | None = None
    after_copy: Callable[[], None] | None = None
    before_verify: Callable[[], None] | None = None
    after_verify: Callable[[], None] | None = None
    before_publish: Callable[[], None] | None = None
    after_publish: Callable[[], None] | None = None
    before_log: Callable[[], None] | None = None
    after_log: Callable[[], None] | None = None


@dataclass(frozen=True)
class MigrationResult:
    """The outcome of a migration run."""

    plan: MigrationPlan
    stage: str
    published: bool
    recovered: bool
    files_copied: int


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_cache_rel(relpath: str) -> bool:
    """Return True if the relative path names a cache component."""
    return any(part.lower() == "cache" or part == "__pycache__" for part in Path(relpath).parts)


def _scan_source(source: Path) -> tuple[list[FileEntry], str]:
    """Walk *source*, rejecting symbolic links, and return the manifest plus a combined digest.

    Cache directories are excluded from the manifest. The combined digest covers
    the relative path and content digest of every migrated file in sorted order.

    """
    files: list[FileEntry] = []
    hasher = hashlib.sha256()
    for root, dirs, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        for directory in dirs:
            if (root_path / directory).is_symlink():
                msg = f"symbolic link rejected: {root_path / directory}"
                raise SymlinkRejectedError(msg)
        dirs[:] = [
            directory for directory in dirs if not _is_cache_rel((root_path / directory).relative_to(source).as_posix())
        ]
        for name in sorted(filenames):
            path = root_path / name
            if path.is_symlink():
                msg = f"symbolic link rejected: {path}"
                raise SymlinkRejectedError(msg)
            relpath = path.relative_to(source).as_posix()
            if _is_cache_rel(relpath):
                continue
            digest = _sha256_file(path)
            files.append(FileEntry(relpath, path.stat().st_size, digest))
            hasher.update(relpath.encode("utf-8"))
            hasher.update(digest.encode("ascii"))
    files.sort(key=lambda entry: entry.relpath)
    return files, hasher.hexdigest()


def _digest_regular_files(path: Path) -> str:
    """Compute a combined digest of regular files under *path*, skipping symlinks and caches."""
    hasher = hashlib.sha256()
    for root, dirs, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirs[:] = [directory for directory in dirs if not (root_path / directory).is_symlink()]
        for name in sorted(filenames):
            file_path = root_path / name
            if file_path.is_symlink():
                continue
            relpath = file_path.relative_to(path).as_posix()
            if _is_cache_rel(relpath):
                continue
            hasher.update(relpath.encode("utf-8"))
            hasher.update(_sha256_file(file_path).encode("ascii"))
    return hasher.hexdigest()


class ConfigMigrator:
    """Migrate an application's configuration directory from *old_appname* to *new_appname*.

    The directory paths are determined by :class:`platformdirs.PlatformDirs`.

    """

    def __init__(  # ruff:ignore[too-many-arguments]
        self,
        old_appname: str,
        new_appname: str,
        *,
        old_appauthor: str | None = None,
        new_appauthor: str | None = None,
        old_version: str | None = None,
        new_version: str | None = None,
        hooks: MigrationHooks | None = None,
    ) -> None:
        """Create a migrator from *old_appname* to *new_appname* with optional authors and versions."""
        self.old_appname = old_appname
        self.new_appname = new_appname
        self.old_appauthor = old_appauthor
        self.new_appauthor = new_appauthor
        self.old_version = old_version
        self.new_version = new_version
        self.hooks = hooks or MigrationHooks()
        self._interrupted = False

    def _resolve_dirs(self) -> tuple[Path, Path]:
        old = PlatformDirs(self.old_appname, self.old_appauthor, self.old_version)
        new = PlatformDirs(self.new_appname, self.new_appauthor, self.new_version)
        return Path(old.user_config_path), Path(new.user_config_path)

    def build_plan(self) -> MigrationPlan:
        """Build the migration plan: resolve directories and scan the source manifest."""
        source_dir, target_dir = self._resolve_dirs()
        if not source_dir.exists():
            msg = f"source directory does not exist: {source_dir}"
            raise MigrationError(msg)
        if not source_dir.is_dir():
            msg = f"source is not a directory: {source_dir}"
            raise MigrationError(msg)
        files, source_digest = _scan_source(source_dir)
        temp_dir = target_dir.parent / f".{self.new_appname}.migration-tmp"
        log_path = target_dir.parent / f".{self.new_appname}.migration.json"
        return MigrationPlan(
            old_appname=self.old_appname,
            new_appname=self.new_appname,
            source_dir=source_dir,
            target_dir=target_dir,
            temp_dir=temp_dir,
            log_path=log_path,
            files=tuple(files),
            source_digest=source_digest,
            run_id=str(uuid.uuid4()),
        )

    def run(self, *, confirm: bool = True) -> MigrationResult:
        """Build the plan and execute the migration."""
        plan = self.build_plan()
        return self.execute(plan, confirm=confirm)

    def execute(self, plan: MigrationPlan, *, confirm: bool = True) -> MigrationResult:
        """Execute a previously built plan, with interruption recovery.

        When *confirm* is true the plan is printed and the user is asked to confirm
        before anything is copied.

        """
        if confirm:
            self._confirm(plan)
        state = self._detect_state(plan)
        self._interrupted = False
        try:
            return self._dispatch(plan, state)
        except BaseException:
            if not self._interrupted and plan.temp_dir.exists():
                shutil.rmtree(plan.temp_dir, ignore_errors=True)
            raise

    def _dispatch(self, plan: MigrationPlan, state: str) -> MigrationResult:
        if state == "published":
            return self._finish_published(plan)
        if state == "conflict":
            msg = f"target already exists and does not match the source: {plan.target_dir}"
            raise TargetExistsError(msg)
        if state == "unpublished_temp":
            return self._publish_temp(plan)
        if state == "temp_corrupt":
            shutil.rmtree(plan.temp_dir)
        return self._copy_verify_publish(plan)

    def _finish_published(self, plan: MigrationPlan) -> MigrationResult:
        self._run_hook("before_log")
        self._write_log(plan, "logged", recovered=True)
        self._run_hook("after_log")
        return MigrationResult(plan=plan, stage="logged", published=True, recovered=True, files_copied=0)

    def _publish_temp(self, plan: MigrationPlan) -> MigrationResult:
        self._run_hook("before_publish")
        self._publish(plan.temp_dir, plan.target_dir)
        self._run_hook("after_publish")
        self._write_log(plan, "published", recovered=True)
        self._verify_target(plan)
        self._write_log(plan, "verified", recovered=True)
        self._run_hook("before_log")
        self._write_log(plan, "logged", recovered=True)
        self._run_hook("after_log")
        return MigrationResult(plan=plan, stage="logged", published=True, recovered=True, files_copied=len(plan.files))

    def _copy_verify_publish(self, plan: MigrationPlan) -> MigrationResult:
        self._write_log(plan, "planned")
        self._run_hook("before_copy")
        self._copy(plan)
        self._run_hook("after_copy")
        self._write_log(plan, "copied")
        self._run_hook("before_verify")
        temp_digest = _digest_regular_files(plan.temp_dir)
        if temp_digest != plan.source_digest:
            msg = f"temporary copy failed verification: {plan.temp_dir}"
            raise VerificationError(msg)
        self._run_hook("after_verify")
        self._write_log(plan, "verified")
        self._run_hook("before_publish")
        self._publish(plan.temp_dir, plan.target_dir)
        self._run_hook("after_publish")
        self._write_log(plan, "published")
        self._verify_target(plan)
        self._write_log(plan, "verified")
        self._run_hook("before_log")
        self._write_log(plan, "logged")
        self._run_hook("after_log")
        return MigrationResult(plan=plan, stage="logged", published=True, recovered=False, files_copied=len(plan.files))

    @staticmethod
    def _verify_target(plan: MigrationPlan) -> None:
        target_digest = _digest_regular_files(plan.target_dir)
        if target_digest != plan.source_digest:
            msg = f"published target failed verification: {plan.target_dir}"
            raise VerificationError(msg)

    @staticmethod
    def _detect_state(plan: MigrationPlan) -> str:
        target_exists = plan.target_dir.exists()
        temp_exists = plan.temp_dir.exists()
        if target_exists and plan.target_dir.is_symlink():
            msg = f"target is a symbolic link: {plan.target_dir}"
            raise SymlinkRejectedError(msg)
        if temp_exists and plan.temp_dir.is_symlink():
            msg = f"temporary directory is a symbolic link: {plan.temp_dir}"
            raise SymlinkRejectedError(msg)
        if target_exists:
            if _digest_regular_files(plan.target_dir) == plan.source_digest:
                return "published"
            return "conflict"
        if temp_exists:
            if _digest_regular_files(plan.temp_dir) == plan.source_digest:
                return "unpublished_temp"
            return "temp_corrupt"
        return "fresh"

    @staticmethod
    def _copy(plan: MigrationPlan) -> None:
        if plan.temp_dir.exists():
            shutil.rmtree(plan.temp_dir)
        plan.temp_dir.mkdir(parents=True)
        for entry in plan.files:
            source = plan.source_dir / entry.relpath
            destination = plan.temp_dir / entry.relpath
            if source.is_symlink():
                msg = f"symbolic link rejected: {source}"
                raise SymlinkRejectedError(msg)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    @staticmethod
    def _publish(temp_dir: Path, target_dir: Path) -> None:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        Path(temp_dir).replace(target_dir)

    def _run_hook(self, name: str) -> None:
        hook = getattr(self.hooks, name)
        if hook is not None:
            try:
                hook()
            except BaseException:
                self._interrupted = True
                raise

    @staticmethod
    def _confirm(plan: MigrationPlan) -> None:
        print(f"Old config directory: {plan.source_dir}")  # ruff:ignore[print]
        print(f"New config directory: {plan.target_dir}")  # ruff:ignore[print]
        print(f"Files to migrate: {len(plan.files)}")  # ruff:ignore[print]
        print(f"Source digest: {plan.source_digest}")  # ruff:ignore[print]
        answer = input("Proceed with migration? [y/N] ").strip().lower()
        if answer != "y":
            msg = "migration cancelled by user"
            raise MigrationError(msg)

    @staticmethod
    def _write_log(plan: MigrationPlan, stage: str, *, recovered: bool = False) -> None:
        existing = _read_log(plan.log_path)
        if existing is not None and _log_identity_matches(existing, plan):
            run_id = existing["run_id"]
            stages = list(existing.get("stages", []))
            files = existing.get(
                "files", [{"relpath": e.relpath, "size": e.size, "digest": e.digest} for e in plan.files]
            )
            source_digest = existing.get("source_digest", plan.source_digest)
            recovered = recovered or bool(existing.get("recovered", False))
        else:
            run_id = plan.run_id
            stages = []
            files = [{"relpath": e.relpath, "size": e.size, "digest": e.digest} for e in plan.files]
            source_digest = plan.source_digest
        now = datetime.now(timezone.utc).isoformat()
        if not stages or stages[-1].get("stage") != stage:
            stages.append({"stage": stage, "at": now})
        data = {
            "log_version": LOG_VERSION,
            "run_id": run_id,
            "old_appname": plan.old_appname,
            "new_appname": plan.new_appname,
            "source_dir": str(plan.source_dir),
            "target_dir": str(plan.target_dir),
            "temp_dir": str(plan.temp_dir),
            "log_path": str(plan.log_path),
            "stage": stage,
            "stages": stages,
            "files": files,
            "source_digest": source_digest,
            "recovered": recovered,
            "updated_at": now,
        }
        plan.log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = plan.log_path.parent / f"{plan.log_path.name}.tmp"
        with Path(tmp_path).open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        Path(tmp_path).replace(plan.log_path)


def _read_log(log_path: Path) -> dict[str, object] | None:
    """Read the migration log, returning None if absent or unreadable."""
    if not log_path.exists():
        return None
    if log_path.is_symlink():
        msg = f"migration log is a symbolic link: {log_path}"
        raise LogIdentityError(msg)
    try:
        with Path(log_path).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _log_identity_matches(data: dict[str, object], plan: MigrationPlan) -> bool:
    """Validate that the log's identity matches the requested migration."""
    if data.get("log_version") != LOG_VERSION:
        msg = f"unsupported migration log version: {data.get('log_version')}"
        raise LogIdentityError(msg)
    if data.get("source_dir") != str(plan.source_dir):
        msg = "migration log source_dir does not match the requested source"
        raise LogIdentityError(msg)
    if data.get("target_dir") != str(plan.target_dir):
        msg = "migration log target_dir does not match the requested target"
        raise LogIdentityError(msg)
    if data.get("temp_dir") != str(plan.temp_dir):
        msg = "migration log temp_dir does not match the requested temp"
        raise LogIdentityError(msg)
    if data.get("log_path") != str(plan.log_path):
        msg = "migration log_path does not match the requested log path"
        raise LogIdentityError(msg)
    return True


__all__ = [
    "ConfigMigrator",
    "FileEntry",
    "LogIdentityError",
    "MigrationError",
    "MigrationHooks",
    "MigrationPlan",
    "MigrationResult",
    "SymlinkRejectedError",
    "TargetExistsError",
    "VerificationError",
]
