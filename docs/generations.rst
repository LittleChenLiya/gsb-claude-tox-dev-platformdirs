.. _generations:

Generation cache manager
========================

``platformdirs.generations`` provides :class:`GenerationManager`, a
lease-guarded generation manager living inside an **explicit cache root**
chosen by the caller.

Layout
------

.. code-block:: text

    <root>/
      owner            # owner token; a different owner refuses to take over
      current          # name of the current generation (atomically replaced)
      gen-1/           # a generation directory
        .gencache-gen      # manager marker (written at begin)
        .gencache-sealed   # seal marker (written before current switches)
        ...payload...
      gen-2/
      my-stuff/        # unmarked user directory: never touched

Lifecycle
---------

1. **Claim** -- ``GenerationManager(root, owner)`` creates the root and writes
   the ``owner`` file.  If the root is already owned by a different owner
   token, :class:`OwnershipError` is raised and nothing is modified.
2. **Begin** -- ``mgr.begin()`` (or the ``mgr.build()`` context manager)
   creates ``gen-N`` with the manager marker.  It is *not* current yet.
3. **Seal** -- ``build.seal()`` writes ``.gencache-sealed`` and only then
   atomically switches ``current`` (write temp file + ``os.replace``).
   If sealing fails, the half-built directory is reclaimed and ``current``
   is left untouched.
4. **Read** -- ``mgr.acquire()`` returns a :class:`Lease` pinning the current
   generation.  A retired generation with active leases is never reclaimed;
   it is deleted only after the last lease is returned.
5. **Reclaim** -- publishing or releasing a lease sweeps retired generations.
   Only directories that (a) match ``gen-*``, (b) carry the manager marker,
   and (c) are not symlinks/junctions are ever removed.  Unmarked user
   directories are preserved forever.
6. **Close** -- ``mgr.close()`` raises :class:`LeaseActiveError` while leases
   are outstanding; it never silently deletes files.  Return every lease
   first, then close.

Example
-------

.. code-block:: python

    from platformdirs.generations import GenerationManager

    mgr = GenerationManager(r"D:\cache\myapp", owner="myapp-writer")

    with mgr.build() as b:                 # failure here keeps old current
        (b.path / "data.bin").write_bytes(payload)
        b.seal()                           # implicit on clean exit

    with mgr.acquire() as lease:           # v1 stays alive while leased
        data = (lease.path / "data.bin").read_bytes()

    mgr.close()                            # raises if any lease is out

Running the tests
-----------------

Point temp dirs at the D: drive and run only this module's tests:

.. code-block:: shell

    set TEMP=D:\Codex\tmp
    set TMP=D:\Codex\tmp
    set PYTHONPATH=src
    python -m pytest tests/test_generations.py --basetemp=D:\Codex\pytest-basetemp\gen -v
