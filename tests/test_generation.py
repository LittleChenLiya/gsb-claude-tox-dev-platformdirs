"""Tests for the generation manager: seal-before-switch, lease-protected reclamation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from platformdirs import (
    GenerationManager,
    Lease,
    LeaseError,
    NoCurrentError,
    OwnershipError,
)
from platformdirs.generation import RETIRED_MARKER, SEALED_MARKER

if TYPE_CHECKING:
    from collections.abc import Iterator


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_publish_seals_and_switches_current(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        gen = mgr.publish(lambda d: _write(d / "data.txt", "hello"))
        assert gen.is_dir()
        assert (gen / "data.txt").read_text() == "hello"
        assert (gen / SEALED_MARKER).is_file()
        assert mgr.current() == gen
        current = root / "current"
        if current.is_symlink():
            assert current.readlink().replace("\\", "/").endswith(gen.name)
        else:
            assert current.read_text(encoding="utf-8").strip() == gen.name


def test_publish_retires_old_and_gc_reclaims(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish(lambda d: _write(d / "f.txt", "v1"))
        v2 = mgr.publish(lambda d: _write(d / "f.txt", "v2"))
        assert mgr.current() == v2
        assert (v1 / RETIRED_MARKER).is_file()
        deleted = mgr.gc()
        assert v1 in deleted
        assert not v1.exists()
        assert v2.exists()
        assert mgr.current() == v2


def test_lease_protects_old_generation_until_released(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish(lambda d: _write(d / "f.txt", "v1"))
        lease = mgr.lease()
        assert isinstance(lease, Lease)
        assert lease.generation == v1
        assert (v1 / "f.txt").read_text() == "v1"
        mgr.publish(lambda d: _write(d / "f.txt", "v2"))
        assert mgr.gc() == []
        assert v1.exists()
        assert (v1 / "f.txt").read_text() == "v1"
        lease.release()
        assert mgr.gc() == [v1]
        assert not v1.exists()


def test_lease_context_manager_releases(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish()
        with mgr.lease() as lease:
            assert lease.generation == v1
            mgr.publish()
            assert mgr.gc() == []
        assert mgr.gc() == [v1]


def test_unmarked_user_directory_is_kept_forever(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish()
        user_dir = root / "generations" / "my-stuff"
        _write(user_dir / "keep.txt", "mine")
        mgr.publish()
        assert mgr.gc() == [v1]
        assert user_dir.exists()
        assert (user_dir / "keep.txt").read_text() == "mine"


def test_failed_publish_does_not_change_current_and_reclaims_dir(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish(lambda d: _write(d / "f.txt", "v1"))

        def fail(d: Path) -> None:
            _write(d / "partial.txt", "partial")
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            mgr.publish(fail)
        assert mgr.current() == v1
        assert not list((root / "generations").glob(".tmp-*"))
        assert (v1 / "f.txt").read_text() == "v1"


def test_failed_publish_after_seal_reclaims_sealed_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish()

        def boom(_gen_id: str) -> None:
            msg = "switch failed"
            raise OSError(msg)

        monkeypatch.setattr(mgr, "_switch_current", boom)
        with pytest.raises(OSError, match="switch failed"):
            mgr.publish()
        # current unchanged, sealed orphan reclaimed
        assert mgr.current() == v1
        leftovers = [p for p in (root / "generations").iterdir() if p.name.startswith(".tmp-")]
        assert leftovers == []


def test_ownership_refuses_takeover(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr1 = GenerationManager(root)
    with pytest.raises(OwnershipError, match="another root owner"):
        GenerationManager(root)
    mgr1.close()
    mgr2 = GenerationManager(root)
    mgr2.close()


def test_close_with_active_leases_requires_return(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = GenerationManager(root)
    mgr.publish()
    lease = mgr.lease()
    with pytest.raises(LeaseError, match="lease"):
        mgr.close()
    assert lease.generation.exists()
    lease.release()
    mgr.close()


def test_close_does_not_delete_files(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish(lambda d: _write(d / "f.txt", "v1"))
        lease = mgr.lease()
        with pytest.raises(LeaseError):
            mgr.close()
        assert v1.exists()
        assert (v1 / "f.txt").read_text() == "v1"
        lease.release()


def test_gc_does_not_follow_symlinks(tmp_path: Path) -> None:
    try:
        tmp_path.joinpath("probe").symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    _write(outside / "important.txt", "do-not-delete")
    with GenerationManager(root) as mgr:
        v1 = mgr.publish()
        link = v1 / "escape"
        link.symlink_to(outside, target_is_directory=True)
        mgr.publish()
        deleted = mgr.gc()
        assert v1 in deleted
        assert not v1.exists()
        assert outside.exists()
        assert (outside / "important.txt").read_text() == "do-not-delete"


def test_rmtree_no_follow_unlinks_symlink_without_descending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic check of the no-follow deletion path: an entry reported as a
    # symlink is unlinked, never descended into.
    gen = tmp_path / "gen"
    gen.mkdir()
    (gen / RETIRED_MARKER).write_text("x", encoding="utf-8")
    sub = gen / "escape"
    sub.mkdir()  # empty real directory; we pretend it is a symlink

    real_is_symlink = Path.is_symlink
    real_iterdir = Path.iterdir
    real_unlink = Path.unlink

    monkeypatch.setattr(Path, "is_symlink", lambda self: self == sub or real_is_symlink(self))

    def guarded_iterdir(self: Path) -> Iterator[Path]:
        if self == sub:
            msg = "must not descend into a symlink"
            raise AssertionError(msg)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    unlinked: list[Path] = []

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        unlinked.append(self)
        if self == sub:
            sub.rmdir()  # remove the empty dir as if unlinking a symlink link
        else:
            real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    GenerationManager._rmtree_no_follow(gen)  # ruff: ignore[private-member-access]
    assert not gen.exists()
    assert sub in unlinked


def test_stale_staging_dir_is_reclaimed(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        mgr.publish()
        stale = root / "generations" / ".tmp-deadbeef"
        _write(stale / "partial.txt", "x")
        assert mgr.gc() == [stale]
        assert not stale.exists()


def test_no_current_raises(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        with pytest.raises(NoCurrentError):
            mgr.current()
        with pytest.raises(NoCurrentError):
            mgr.lease()


def test_empty_publish_is_sealed(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        gen = mgr.publish()
        assert gen.is_dir()
        assert (gen / SEALED_MARKER).is_file()
        assert mgr.current() == gen


def test_reopen_after_close(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        v1 = mgr.publish()
    with GenerationManager(root) as mgr:
        assert mgr.current() == v1
        v2 = mgr.publish()
        assert mgr.current() == v2
        assert mgr.gc() == [v1]


def test_closed_manager_rejects_operations(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    mgr = GenerationManager(root)
    mgr.publish()
    mgr.close()
    with pytest.raises(Exception, match="closed"):
        mgr.publish()
    with pytest.raises(Exception, match="closed"):
        mgr.current()


def test_lease_release_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    with GenerationManager(root) as mgr:
        mgr.publish()
        lease = mgr.lease()
        lease.release()
        lease.release()
        mgr.publish()
        assert mgr.gc()
