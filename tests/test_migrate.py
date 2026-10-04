from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import platformdirs.windows as winmod
from platformdirs import (
    ConfigMigrator,
    LogIdentityError,
    MigrationHooks,
    SymlinkRejectedError,
    TargetExistsError,
)


@pytest.fixture
def win_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the Windows folder resolution at a temp base under the pytest temp dir."""
    base = tmp_path / "winroot"
    base.mkdir()
    monkeypatch.setattr(winmod, "get_win_folder", lambda _csidl: str(base))
    return base


def _make_old_config(base: Path, appauthor: str, appname: str) -> Path:
    old_dir = base / appauthor / appname
    old_dir.mkdir(parents=True)
    return old_dir


def _migrator(
    old: str = "OldApp", new: str = "NewApp", old_author: str = "OldVendor", new_author: str = "NewVendor"
) -> ConfigMigrator:
    return ConfigMigrator(old, new, old_appauthor=old_author, new_appauthor=new_author)


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_basic_migration_copies_three_files(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "settings.ini").write_text("settings=1")
    (old_dir / "profile.json").write_text("{}")
    (old_dir / "notes.txt").write_text("remember")

    plan = _migrator().build_plan()
    result = _migrator().execute(plan, confirm=False)

    assert result.published is True
    assert result.recovered is False
    assert result.files_copied == 3
    assert _tree(plan.target_dir) == ["notes.txt", "profile.json", "settings.ini"]
    assert not plan.temp_dir.exists()
    assert plan.log_path.exists()
    assert _tree(old_dir) == ["notes.txt", "profile.json", "settings.ini"]


def test_cache_excluded(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "settings.ini").write_text("settings=1")
    cache_dir = old_dir / "cache"
    cache_dir.mkdir()
    (cache_dir / "big.bin").write_text("x" * 100)
    pycache = old_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.pyc").write_text("pyc")

    plan = _migrator().build_plan()
    result = _migrator().execute(plan, confirm=False)

    assert result.files_copied == 1
    assert _tree(plan.target_dir) == ["settings.ini"]
    assert (old_dir / "cache" / "big.bin").exists()


def test_symlink_rejected(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "real.txt").write_text("real")
    try:
        (old_dir / "link.txt").symlink_to(old_dir / "real.txt")
    except OSError as error:
        pytest.skip(f"symlink creation not supported: {error}")

    migrator = _migrator()
    with pytest.raises(SymlinkRejectedError):
        migrator.build_plan()


def test_target_conflict_rejected(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")
    target_dir = win_base / "NewVendor" / "NewApp"
    target_dir.mkdir(parents=True)
    (target_dir / "foreign.txt").write_text("foreign")

    plan = _migrator().build_plan()
    with pytest.raises(TargetExistsError):
        _migrator().execute(plan, confirm=False)
    assert _tree(target_dir) == ["foreign.txt"]
    assert not plan.temp_dir.exists()


def test_second_copy_failure_cleans_temp(win_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")
    (old_dir / "b.txt").write_text("bbb")
    (old_dir / "c.txt").write_text("ccc")

    plan = _migrator().build_plan()
    real_copy2 = shutil.copy2
    calls: list[Path] = []

    def failing_copy2(src: Path, dst: Path, *args: object, **kwargs: object) -> object:
        calls.append(Path(src))
        if len(calls) == 2:
            msg = "injected copy failure"
            raise OSError(msg)
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr("platformdirs.migrate.shutil.copy2", failing_copy2)
    with pytest.raises(OSError, match="injected copy failure"):
        _migrator().execute(plan, confirm=False)

    assert _tree(old_dir) == ["a.txt", "b.txt", "c.txt"]
    assert not plan.target_dir.exists()
    assert not plan.temp_dir.exists()


def test_interrupt_after_publish_recovery_keeps_target(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")
    (old_dir / "b.txt").write_text("bbb")
    (old_dir / "c.txt").write_text("ccc")

    migrator = _migrator()
    plan = migrator.build_plan()

    def interrupt() -> None:
        msg = "injected interrupt after publish"
        raise InterruptedError(msg)

    migrator.hooks = MigrationHooks(after_publish=interrupt)
    with pytest.raises(InterruptedError):
        migrator.execute(plan, confirm=False)

    assert plan.target_dir.exists()
    assert not plan.temp_dir.exists()
    log = json.loads(plan.log_path.read_text())
    assert log["stage"] == "verified"
    assert [s["stage"] for s in log["stages"]] == ["planned", "copied", "verified"]

    inode_before = plan.target_dir.stat().st_ino
    publish_attempts: list[int] = []
    migrator.hooks = MigrationHooks(before_publish=lambda: publish_attempts.append(1))
    result = migrator.execute(plan, confirm=False)

    assert result.recovered is True
    assert result.published is True
    assert publish_attempts == []
    assert plan.target_dir.stat().st_ino == inode_before
    assert _tree(plan.target_dir) == ["a.txt", "b.txt", "c.txt"]
    log = json.loads(plan.log_path.read_text())
    assert log["stage"] == "logged"
    assert log["recovered"] is True


def test_interrupt_before_publish_recovers_temp(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")
    (old_dir / "b.txt").write_text("bbb")

    migrator = _migrator()
    plan = migrator.build_plan()

    def interrupt() -> None:
        msg = "injected interrupt before publish"
        raise InterruptedError(msg)

    migrator.hooks = MigrationHooks(before_publish=interrupt)
    with pytest.raises(InterruptedError):
        migrator.execute(plan, confirm=False)

    assert not plan.target_dir.exists()
    assert plan.temp_dir.exists()

    migrator.hooks = MigrationHooks()
    result = migrator.execute(plan, confirm=False)

    assert result.recovered is True
    assert result.published is True
    assert _tree(plan.target_dir) == ["a.txt", "b.txt"]
    assert not plan.temp_dir.exists()


def test_log_identity_mismatch_rejected(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")

    migrator = _migrator()
    plan = migrator.build_plan()
    migrator.execute(plan, confirm=False)

    log = json.loads(plan.log_path.read_text())
    log["target_dir"] = str(win_base / "Elsewhere" / "Other")
    plan.log_path.write_text(json.dumps(log))

    with pytest.raises(LogIdentityError):
        migrator.execute(plan, confirm=False)


def test_log_version_mismatch_rejected(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")

    migrator = _migrator()
    plan = migrator.build_plan()
    migrator.execute(plan, confirm=False)

    log = json.loads(plan.log_path.read_text())
    log["log_version"] = 99
    plan.log_path.write_text(json.dumps(log))

    with pytest.raises(LogIdentityError):
        migrator.execute(plan, confirm=False)


def test_recovery_does_not_overwrite_existing_target(win_base: Path) -> None:
    old_dir = _make_old_config(win_base, "OldVendor", "OldApp")
    (old_dir / "a.txt").write_text("aaa")

    migrator = _migrator()
    plan = migrator.build_plan()
    migrator.execute(plan, confirm=False)

    (plan.target_dir / "marker.txt").write_text("published")

    with pytest.raises(TargetExistsError):
        migrator.execute(plan, confirm=False)
    assert (plan.target_dir / "marker.txt").read_text() == "published"
