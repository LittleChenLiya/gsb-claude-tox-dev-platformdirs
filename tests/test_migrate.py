"""Tests for the crash-safe config directory migration executor."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from platformdirs import PlatformDirs
from platformdirs.migrate import (
    LOG_FILE_NAME,
    LOG_SCHEMA_VERSION,
    PHASE_ABORT,
    PHASE_CONFIRM,
    PHASE_DONE,
    PHASE_PLAN,
    PHASE_PUBLISH,
    PHASE_RECOVER,
    PHASE_STAGE,
    PHASE_VERIFY,
    CopyFailed,
    LogPathError,
    MigrationExecutor,
    SymlinkRefused,
    TargetConflict,
    digest_of_tree,
    validate_log_path,
)

THREE_FILES = {f"file-{index}.txt": f"content-{index}" for index in range(3)}


def make_executor(tmp_path: Path, **kwargs) -> MigrationExecutor:
    """Create an executor with a three-file source config directory."""
    source = tmp_path / "old-config"
    target = tmp_path / "new-config"
    source.mkdir(parents=True, exist_ok=True)
    for name, text in THREE_FILES.items():
        (source / name).write_text(text, encoding="utf-8")
    kwargs.setdefault("log_dir", tmp_path / "logs")
    return MigrationExecutor(source, target, app_old="OldApp", app_new="NewApp", **kwargs)


def read_log(log_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")


def test_plan_confirm_and_migrate_three_files(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    seen_plans = []
    result = executor.migrate(confirm=lambda plan: seen_plans.append(plan) or True)

    assert result.status == "migrated"
    # The plan was presented for confirmation before anything was copied.
    assert len(seen_plans) == 1
    plan = seen_plans[0]
    assert sorted(entry.relpath for entry in plan.entries) == sorted(THREE_FILES)
    assert plan.source == tmp_path / "old-config"
    assert plan.target == tmp_path / "new-config"

    # All three files arrived at the target.
    for name, text in THREE_FILES.items():
        assert (plan.target / name).read_text(encoding="utf-8") == text
    # Source preserved (remove_source defaults to False), staging is gone.
    assert digest_of_tree(plan.source) == plan.digest
    assert not plan.staging.exists()

    # Versioned log: every phase recorded, every record schema-versioned.
    assert result.log_path.name == LOG_FILE_NAME == "migration.v1.jsonl"
    records = read_log(result.log_path)
    phases = [record["phase"] for record in records]
    assert phases == [
        PHASE_PLAN,
        PHASE_CONFIRM,
        PHASE_STAGE,
        PHASE_VERIFY,
        PHASE_PUBLISH,
        PHASE_DONE,
    ]
    assert all(record["version"] == LOG_SCHEMA_VERSION for record in records)
    assert all(record["digest"] == plan.digest for record in records)


def test_staging_directory_is_sibling_of_target(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    plan = executor.build_plan()
    assert plan.staging.parent == plan.target.parent
    assert plan.staging.name == "new-config.migrating"


def test_declined_confirmation_touches_nothing(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    result = executor.migrate(confirm=lambda plan: False)
    assert result.status == "declined"
    assert not (tmp_path / "new-config").exists()
    assert not (tmp_path / "new-config.migrating").exists()
    assert digest_of_tree(tmp_path / "old-config") == result.plan.digest
    records = read_log(result.log_path)
    assert [r["phase"] for r in records] == [PHASE_PLAN, PHASE_CONFIRM]
    assert records[-1]["details"]["accepted"] is False


def test_injected_second_copy_failure_cleans_up(tmp_path: Path) -> None:
    calls: list[Path] = []

    def flaky_copy(src: Path, dst: Path) -> None:
        calls.append(src)
        if len(calls) == 2:
            msg = "injected failure on the second copy"
            raise OSError(msg)
        shutil.copy2(src, dst)

    executor = make_executor(tmp_path, copy_func=flaky_copy)
    source_digest_before = digest_of_tree(tmp_path / "old-config")

    with pytest.raises(CopyFailed):
        executor.migrate()

    # Exactly one file was copied before the injected failure.
    assert len(calls) == 2
    # Source is untouched, the target never appeared, staging was cleaned.
    assert digest_of_tree(tmp_path / "old-config") == source_digest_before
    assert not (tmp_path / "new-config").exists()
    assert not (tmp_path / "new-config.migrating").exists()
    # The abort was recorded in the versioned log.
    records = read_log(tmp_path / "logs" / LOG_FILE_NAME)
    phases = [record["phase"] for record in records]
    assert phases == [PHASE_PLAN, PHASE_CONFIRM, PHASE_ABORT]
    assert "injected failure" in records[-1]["details"]["error"]


def test_symlink_in_source_is_refused(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    make_symlink(tmp_path / "old-config" / "link.txt", tmp_path / "old-config" / "file-0.txt")
    with pytest.raises(SymlinkRefused):
        executor.build_plan()


def test_symlinked_log_path_is_refused(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    real_log = tmp_path / "real-log.jsonl"
    real_log.write_text("", encoding="utf-8")
    make_symlink(log_dir / LOG_FILE_NAME, real_log)
    with pytest.raises(LogPathError):
        executor.build_plan()


def test_existing_target_with_foreign_content_is_a_conflict(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    target = tmp_path / "new-config"
    target.mkdir()
    (target / "other.txt").write_text("foreign", encoding="utf-8")
    source_digest_before = digest_of_tree(tmp_path / "old-config")

    with pytest.raises(TargetConflict):
        executor.migrate()

    # Neither the source nor the pre-existing target was modified.
    assert digest_of_tree(tmp_path / "old-config") == source_digest_before
    assert (target / "other.txt").read_text(encoding="utf-8") == "foreign"
    assert not (tmp_path / "new-config.migrating").exists()


def test_crash_after_publish_before_log_recovers_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = make_executor(tmp_path)
    original_append = MigrationExecutor._append_log

    def crash_on_publish(self, plan, phase, **details):
        if phase == PHASE_PUBLISH:
            msg = "simulated crash between publish and log write"
            raise RuntimeError(msg)
        return original_append(self, plan, phase, **details)

    monkeypatch.setattr(MigrationExecutor, "_append_log", crash_on_publish)
    with pytest.raises(RuntimeError, match="simulated crash"):
        executor.migrate()

    # The publish rename happened; the publish log record did not.
    target = tmp_path / "new-config"
    assert target.is_dir()
    assert not (tmp_path / "new-config.migrating").exists()
    log_path = tmp_path / "logs" / LOG_FILE_NAME
    phases = [record["phase"] for record in read_log(log_path)]
    assert PHASE_PUBLISH not in phases
    assert PHASE_STAGE in phases and PHASE_VERIFY in phases

    # Recovery run: copying must not happen again.
    def forbidden_copy(src: Path, dst: Path) -> None:
        raise AssertionError("recovery must not copy or overwrite anything")

    recovered = make_executor(tmp_path, copy_func=forbidden_copy).migrate()
    assert recovered.status == "already-published"
    # The existing target was recognised through its digest summary.
    assert recovered.digest == digest_of_tree(target)
    assert recovered.details["target_digest"] == recovered.digest
    for name, text in THREE_FILES.items():
        assert (target / name).read_text(encoding="utf-8") == text
    # The recovery itself was logged, without any new publish record.
    records = read_log(log_path)
    recover_records = [r for r in records if r["phase"] == PHASE_RECOVER]
    assert recover_records[-1]["details"]["action"] == "already-published"
    assert PHASE_PUBLISH not in [r["phase"] for r in records]


def test_stale_staging_from_interrupted_run_is_cleaned(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    plan = executor.build_plan()
    # Simulate unpublished temporary content of an interrupted earlier run.
    plan.staging.mkdir()
    (plan.staging / "junk.txt").write_text("junk", encoding="utf-8")

    result = executor.migrate()
    assert result.status == "migrated"
    assert not (tmp_path / "new-config.migrating").exists()
    assert not (result.plan.target / "junk.txt").exists()
    records = read_log(result.log_path)
    recover = [r for r in records if r["phase"] == PHASE_RECOVER]
    assert recover[0]["details"]["action"] == "removed-stale-staging"


def test_cache_directory_is_never_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Redirect every platform directory root into the fixture directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    if os.name == "nt":
        win_folders = {
            "CSIDL_APPDATA": str(tmp_path / "roaming"),
            "CSIDL_LOCAL_APPDATA": str(tmp_path / "local"),
            "CSIDL_COMMON_APPDATA": str(tmp_path / "common"),
        }
        monkeypatch.setattr(
            "platformdirs.windows.get_win_folder",
            lambda csidl: win_folders.get(csidl, str(tmp_path / "misc")),
        )

    old = PlatformDirs("OldApp", "DemoCo")
    new = PlatformDirs("NewApp", "DemoCo")
    old_config = Path(old.user_config_path)
    old_config.mkdir(parents=True)
    (old_config / "settings.ini").write_text("x=1", encoding="utf-8")
    old_cache = Path(old.user_cache_path)
    old_cache.mkdir(parents=True)
    cache_file = old_cache / "cache.db"
    cache_file.write_text("cached-bytes", encoding="utf-8")

    executor = MigrationExecutor.from_platformdirs(old, new)
    plan = executor.build_plan()
    assert plan.source == old_config
    assert plan.target == Path(new.user_config_path)
    # The cache file is not part of the plan.
    assert [entry.relpath for entry in plan.entries] == ["settings.ini"]

    result = executor.migrate()
    assert result.status == "migrated"
    # The cache stayed in place and nothing cache-related was migrated.
    assert cache_file.read_text(encoding="utf-8") == "cached-bytes"
    assert not (plan.target / "cache.db").exists()
    assert not (plan.target / "Cache").exists()  # Windows nests the cache dir
    assert (plan.target / "settings.ini").read_text(encoding="utf-8") == "x=1"


def test_log_path_identity_validation(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    good = log_dir / LOG_FILE_NAME
    assert validate_log_path(good, log_dir) == good

    # Wrong file name (not the versioned migration log name).
    with pytest.raises(LogPathError):
        validate_log_path(log_dir / "migration.jsonl", log_dir)
    # Not directly inside the log directory.
    with pytest.raises(LogPathError):
        validate_log_path(tmp_path / "elsewhere" / LOG_FILE_NAME, log_dir)
    # Existing log path that is a directory, not a regular file.
    other_dir = tmp_path / "other"
    (other_dir / LOG_FILE_NAME).mkdir(parents=True)
    with pytest.raises(LogPathError):
        validate_log_path(other_dir / LOG_FILE_NAME, other_dir)


def test_remove_source_only_after_publish(tmp_path: Path) -> None:
    executor = make_executor(tmp_path, remove_source=True)
    result = executor.migrate()
    assert result.status == "migrated"
    assert not (tmp_path / "old-config").exists()
    assert digest_of_tree(tmp_path / "new-config") == result.digest
    records = read_log(result.log_path)
    done = [r for r in records if r["phase"] == PHASE_DONE]
    assert done[-1]["details"]["source_removed"] is True
    # The publish was logged before the source removal was logged.
    phases = [r["phase"] for r in records]
    assert phases.index(PHASE_PUBLISH) < phases.index(PHASE_DONE)


def test_digest_of_tree_is_deterministic(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)
    plan = executor.build_plan()
    assert digest_of_tree(plan.source) == plan.digest == digest_of_tree(plan.source)


def test_symlink_refusal_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch is_symlink so the refusal branch is covered even on platforms
    # where creating real symlinks requires extra privileges.
    executor = make_executor(tmp_path)
    victim = tmp_path / "old-config" / "file-1.txt"
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        return self == victim or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(SymlinkRefused):
        executor.build_plan()


def test_symlinked_log_path_refusal_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = make_executor(tmp_path)
    victim = tmp_path / "logs" / LOG_FILE_NAME
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        return self == victim or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(LogPathError):
        executor.build_plan()
