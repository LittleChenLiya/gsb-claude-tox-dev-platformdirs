"""Tests for the generation manager (platformdirs.generations)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from platformdirs.generations import (
    CURRENT_FILE,
    GEN_MARKER,
    SEAL_MARKER,
    GenerationError,
    GenerationManager,
    LeaseActiveError,
    OwnershipError,
)

OWNER = "test-owner"


def make_manager(root: Path, owner: str = OWNER) -> GenerationManager:
    return GenerationManager(root, owner)


def publish(root: Path, name: str, owner: str = OWNER) -> GenerationManager:
    mgr = make_manager(root, owner)
    with mgr.build() as b:
        (b.path / "payload.txt").write_text(name, encoding="utf-8")
    return mgr


def test_lease_blocks_reclaim_until_released(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build() as b:
        (b.path / "payload.txt").write_text("v1", encoding="utf-8")
    v1_name = (root / CURRENT_FILE).read_text(encoding="utf-8").strip()

    # Reader takes a lease on v1.
    lease = mgr.acquire()
    assert lease.generation == v1_name

    # Publish v2 while the v1 lease is active.
    with mgr.build() as b:
        (b.path / "payload.txt").write_text("v2", encoding="utf-8")
    v2_name = (root / CURRENT_FILE).read_text(encoding="utf-8").strip()
    assert v2_name != v1_name

    # v1 must still be readable and present on disk.
    assert (lease.path / "payload.txt").read_text(encoding="utf-8") == "v1"
    assert (root / v1_name).is_dir()

    # Explicit reclaim must not touch the leased generation either.
    mgr.reclaim()
    assert (root / v1_name).is_dir()

    # Releasing the lease reclaims the retired generation.
    lease.release()
    assert not (root / v1_name).exists()
    assert (root / v2_name).is_dir()
    mgr.close()


def test_unmarked_user_directory_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    user_dir = root / "gen-99"  # looks like a generation but has no marker
    user_dir.mkdir()
    (user_dir / "keep.txt").write_text("mine", encoding="utf-8")
    plain_dir = root / "my-stuff"
    plain_dir.mkdir()

    with mgr.build():
        pass
    with mgr.build():
        pass
    mgr.reclaim()

    assert (user_dir / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert plain_dir.is_dir()
    mgr.close()


def test_failed_build_keeps_current_and_reclaims_attempt(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build() as b:
        (b.path / "payload.txt").write_text("v1", encoding="utf-8")
    before = (root / CURRENT_FILE).read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"), mgr.build() as b:
        failed_path = b.path
        (b.path / "partial.txt").write_text("junk", encoding="utf-8")
        raise RuntimeError("boom")

    # current unchanged, failed attempt directory reclaimed.
    assert (root / CURRENT_FILE).read_text(encoding="utf-8") == before
    assert not failed_path.exists()
    mgr.close()


def test_failed_seal_reclaims_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build():
        pass
    before = (root / CURRENT_FILE).read_text(encoding="utf-8")

    build = mgr.begin()
    attempt = build.path
    # Break sealing by making the seal marker path a directory.
    (attempt / SEAL_MARKER).mkdir()
    with pytest.raises(OSError):
        build.seal()
    assert not attempt.exists()
    assert (root / CURRENT_FILE).read_text(encoding="utf-8") == before
    mgr.close()


def test_sealed_marker_written_before_current_switch(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build() as b:
        assert not (root / CURRENT_FILE).exists()  # not switched during build
        assert not (b.path / SEAL_MARKER).exists()
    name = (root / CURRENT_FILE).read_text(encoding="utf-8").strip()
    assert (root / name / SEAL_MARKER).is_file()
    assert (root / name / GEN_MARKER).is_file()
    mgr.close()


def test_other_owner_refused(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root, owner="alice")
    mgr.close()
    with pytest.raises(OwnershipError):
        make_manager(root, owner="bob")
    # Same owner may reopen.
    again = make_manager(root, owner="alice")
    again.close()


def test_close_requires_leases_returned(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build() as b:
        (b.path / "payload.txt").write_text("v1", encoding="utf-8")
    lease = mgr.acquire()
    with pytest.raises(LeaseActiveError):
        mgr.close()
    # Nothing was silently deleted.
    assert (lease.path / "payload.txt").read_text(encoding="utf-8") == "v1"
    lease.release()
    mgr.close()
    with pytest.raises(GenerationError):
        mgr.acquire()


def test_symlink_generation_not_followed_or_deleted(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build():
        pass
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "data.txt").write_text("precious", encoding="utf-8")
    link = root / "gen-50"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        # No symlink privilege (common on Windows): use a junction instead.
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode != 0 or not link.exists():  # pragma: no cover
            mgr.close()
            pytest.skip("neither symlinks nor junctions available")
    mgr.reclaim()
    assert link.exists() or link.is_symlink()
    assert (target / "data.txt").read_text(encoding="utf-8") == "precious"
    mgr.close()


def test_acquire_without_publish_raises(tmp_path: Path) -> None:
    mgr = make_manager(tmp_path / "cache")
    with pytest.raises(GenerationError, match="no generation"):
        mgr.acquire()
    mgr.close()


def test_lease_context_manager(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = make_manager(root)
    with mgr.build() as b:
        (b.path / "payload.txt").write_text("v1", encoding="utf-8")
    with mgr.acquire() as lease:
        assert mgr.active_leases() == 1
        assert (lease.path / "payload.txt").read_text(encoding="utf-8") == "v1"
    assert mgr.active_leases() == 0
    mgr.close()
