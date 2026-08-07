"""Does an encrypted database actually keep your text off the disk?

WHY THIS EXISTS. Released engine 0.13.1 — and every version back to roughly
0.7 — wrote full record plaintext into `oplog.payload` on encrypted databases.
`memories.text` was proper ciphertext, entities and FTS were clean, and
`is_encrypted` read True the whole time. The write-ahead projection carried the
payload across the encryption boundary without declaring it.

The property that makes this worth a permanent instrument is not the bug, it
is the SHAPE of the bug: **the verification surface confirmed a guarantee the
storage did not provide.** An operator doing the responsible thing — enable
encryption, check the flag — got an affirmative answer and plaintext on disk.

SO THIS DOES NOT ASK "is the payload encrypted". It writes a distinctive canary
string, then scans the RAW BYTES of every file the database touches, and only
afterwards localises which table holds it. Asking a narrower question means
knowing in advance where to look — and not knowing where to look is the entire
situation this is for. Run against a version you have not audited and it will
find the column for you.

Exit code is 1 if any plaintext is found, so it can gate a release.

    python benchmarks/encryption_canary.py            # bundled embedder, dim 64
    python benchmarks/encryption_canary.py --dim 128
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Distinctive enough that a hit cannot be coincidence, and not a word that
# would appear in an index, a schema, or an embedder's vocabulary.
CANARY = "CANARY_KLXQ_9f3a_SECRET_PAYLOAD_do_not_leak"
DRAIN_SECONDS = 6


def _scan_raw_bytes(directory: str) -> dict[str, int]:
    """Every file the db touched, not just the one we named — sidecars (-wal,
    -shm) are where a payload hides from someone checking only the main file."""
    hits: dict[str, int] = {}
    for path in glob.glob(os.path.join(directory, "*")):
        try:
            blob = Path(path).read_bytes()
        except OSError:
            continue
        n = blob.count(CANARY.encode())
        if n:
            hits[os.path.basename(path)] = n
    return hits


def _localise(db_path: str) -> dict[str, list[str]]:
    """Only run AFTER the byte scan has already answered yes/no. Naming the
    table is for the fix; finding the leak must not depend on guessing it."""
    found: dict[str, list[str]] = {}
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return found
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        try:
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
        except sqlite3.Error:
            continue
        for c in cols:
            try:
                n = con.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE CAST({c} AS TEXT) LIKE ?",
                    ("%" + CANARY + "%",)).fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                found.setdefault(t, []).append(f"{c} x{n}")
    con.close()
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dim", type=int, default=64,
                    help="embedding dim; must match the embedder in use")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    import yantrikdb

    # CONTROL FIRST. A byte scan that finds nothing proves nothing unless the
    # same scan, on the same build, finds a canary it SHOULD find. Without
    # this, a write that silently failed, a drain that was too short, or a
    # changed storage layout all report "clean" — the scan measuring nothing
    # and saying so in the language of success.
    control_dir = tempfile.mkdtemp()
    control_path = os.path.join(control_dir, "plain.db")
    plain = YantrikDB(control_path, embedding_dim=args.dim)
    plain.record(CANARY, memory_type="semantic", namespace="canary")
    time.sleep(DRAIN_SECONDS)
    del plain
    control_hits = _scan_raw_bytes(control_dir)
    if not control_hits:
        print(json.dumps({
            "engine": yantrikdb.__version__,
            "verdict": "INCONCLUSIVE — the control failed",
            "detail": ("an UNENCRYPTED database did not show the canary in its "
                       "raw bytes, so this scan cannot detect a leak on this "
                       "build. Any 'clean' result here would be the instrument "
                       "measuring nothing. Fix the control before trusting a "
                       "pass."),
        }, indent=2))
        return 2

    workdir = tempfile.mkdtemp()
    db_path = os.path.join(workdir, "canary.db")
    db = YantrikDB(db_path, embedding_dim=args.dim,
                   encryption_key=bytes(range(32)))
    claimed = bool(getattr(db, "is_encrypted", False))
    db.record(CANARY, memory_type="semantic", namespace="canary")
    time.sleep(DRAIN_SECONDS)     # the oplog is written asynchronously
    del db

    raw = _scan_raw_bytes(workdir)
    where = _localise(db_path) if raw else {}
    leaked = bool(raw)

    print(json.dumps({
        "engine": yantrikdb.__version__,
        "control_unencrypted_canary_found": control_hits,
        "control_note": ("the same scan on an UNENCRYPTED db on this build — it "
                         "must find the canary, or a clean result below means "
                         "nothing"),
        "is_encrypted_reports": claimed,
        "canary_in_raw_file_bytes": raw,
        "plaintext_located_in": where,
        "verdict": ("PLAINTEXT LEAK — the database reports itself encrypted and "
                    "the canary is readable in its raw bytes" if leaked and claimed
                    else "PLAINTEXT PRESENT and encryption was not claimed"
                    if leaked else
                    "clean — no plaintext in any file the database touched"),
        "note": ("is_encrypted alone was not a sufficient check on affected "
                 "versions; that is why this scans bytes rather than asking "
                 "the database about itself"),
    }, indent=2))
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
