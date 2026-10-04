"""Small migration demo: rename an app's config directory with interruption recovery.

Run directly with ``python demo/migrate_demo.py``. The demo creates a temporary
workspace on the current drive, builds an old configuration directory with three
files (plus a cache file that must be excluded), runs the migration, then
simulates an interruption after publish and re-runs to show that the published
target is recognized and not overwritten.

"""

# ruff: file-ignore[print]

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import platformdirs.windows as winmod
from platformdirs import ConfigMigrator, MigrationHooks


def _make_workspace() -> Path:
    worktree = Path(__file__).resolve().parent.parent
    return Path(tempfile.mkdtemp(prefix="mig_demo_", dir=worktree))


def _setup_old_config(base: Path) -> Path:
    old_dir = base / "OldVendor" / "OldApp"
    old_dir.mkdir(parents=True)
    (old_dir / "settings.ini").write_text("[settings]\nname=demo\n", encoding="utf-8")
    (old_dir / "profiles.json").write_text('{"profiles": ["default"]}\n', encoding="utf-8")
    (old_dir / "notes.txt").write_text("remember to migrate\n", encoding="utf-8")
    cache_dir = old_dir / "cache"
    cache_dir.mkdir()
    (cache_dir / "thumbnail.bin").write_bytes(b"\x00\x01\x02" * 64)
    return old_dir


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def main() -> None:
    """Run the migration demo: migrate, interrupt, and recover."""
    base = _make_workspace()
    try:
        winmod.get_win_folder = lambda _csidl: str(base)
        old_dir = _setup_old_config(base)
        print(f"Old config directory: {old_dir}")
        print(f"Old tree: {_tree(old_dir)}")

        migrator = ConfigMigrator("OldApp", "NewApp", old_appauthor="OldVendor", new_appauthor="NewVendor")
        plan = migrator.build_plan()
        print(f"\nNew config directory: {plan.target_dir}")
        print(f"Files to migrate (cache excluded): {[e.relpath for e in plan.files]}")
        print(f"Source digest: {plan.source_digest}")

        print("\n--- Running migration ---")
        result = migrator.execute(plan, confirm=False)
        print(f"Result: stage={result.stage}, published={result.published}, recovered={result.recovered}")
        print(f"New tree: {_tree(plan.target_dir)}")
        print(f"Temp remains: {plan.temp_dir.exists()}")

        print("\n--- Simulating interruption after publish ---")

        def interrupt() -> None:
            msg = "power loss after publish"
            raise InterruptedError(msg)

        migrator.hooks = MigrationHooks(after_publish=interrupt)
        try:
            migrator.execute(plan, confirm=False)
        except InterruptedError:
            print("Process interrupted after publish, before log write.")
        print(f"Target exists: {plan.target_dir.exists()}")
        print(f"Temp exists: {plan.temp_dir.exists()}")

        print("\n--- Re-running (recovery) ---")
        migrator.hooks = MigrationHooks()
        publish_attempts: list[int] = []
        migrator.hooks.before_publish = lambda: publish_attempts.append(1)
        result = migrator.execute(plan, confirm=False)
        print(f"Result: stage={result.stage}, published={result.published}, recovered={result.recovered}")
        print(f"Publish attempts during recovery: {len(publish_attempts)} (target not overwritten)")
        print(f"New tree: {_tree(plan.target_dir)}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        print(f"\nCleaned workspace: {base}")


if __name__ == "__main__":
    main()
