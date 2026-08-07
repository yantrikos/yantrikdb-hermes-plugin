"""Does the migration ERASE the plaintext, or only stop writing new plaintext?

WHY THIS EXISTS SEPARATELY FROM encryption_canary.py. That one writes to a
freshly-created encrypted database and scans the bytes. It passed on 0.13.2 and
0.13.3 — correctly, because the fresh-write path was genuinely fixed. It could
not have failed on the defect this file exists for, and I reported those passes
without naming that limit.

The defect: sealing an existing row is an UPDATE. Ciphertext goes to a new
page and the old page is FREED, not overwritten. So after migrating a database
written by a pre-fix engine, every live row is sealed, `oplog_plaintext_rows()`
returns 0, and the plaintext is still in the file until SQLite reuses the page.
Measured on 200 rows:

    migrated by 0.13.3   oplog_plaintext_rows 0    canary in raw bytes: 3
    migrated by 0.13.4   oplog_plaintext_rows 0    canary in raw bytes: 0

Both counters read 0. Only the bytes tell them apart — which is why the count
had to grow a docstring saying it counts ROWS, NOT BYTES.

HOW IT WORKS. Two interpreters, one database file: a PRE-FIX engine writes the
rows, then the CANDIDATE engine opens the same file and runs its migration.
Requires two environments; that is the point, and it is why this could not be
folded into the single-engine instrument.

THE CONTROL IS BUILT IN. If the pre-fix write leaves no plaintext, the scan
cannot detect the freed-page case at all, and the run reports INCONCLUSIVE
rather than passing — a negative result must demonstrate its own sensitivity,
in the same run, on the same fixture.

    python benchmarks/encryption_migration_canary.py <prefix_python> <candidate_python> [rows]

Verify the pair by also running it with a KNOWN-BAD candidate (0.13.2/0.13.3):
it must report RESIDUAL. A run that only ever shows green has not been shown to
go red.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CANARY = "CANARY_MIGR_7b2e_PREFIX_PLAINTEXT_row"
KEY_EXPR = "bytes(range(32))"
WRITE_DRAIN = 8
MIGRATE_DRAIN = 12


def _scan(directory: str) -> dict[str, int]:
    hits = {}
    for f in glob.glob(os.path.join(directory, "*")):
        try:
            n = Path(f).read_bytes().count(CANARY.encode())
        except OSError:
            continue
        if n:
            hits[os.path.basename(f)] = n
    return hits


def _run(python: str, code: str, cwd: str) -> str:
    out = subprocess.run([python, "-c", code], capture_output=True, text=True,
                         cwd=cwd)
    if out.returncode != 0:
        raise SystemExit(f"subprocess failed:\n{out.stderr[:600]}")
    return out.stdout.strip().splitlines()[-1]


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__.strip().splitlines()[-4])
    prefix_py, candidate_py = sys.argv[1], sys.argv[2]
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    work = tempfile.mkdtemp()
    db = os.path.join(work, "m.db")

    pre_ver = _run(prefix_py, f'''
import time
from yantrikdb._yantrikdb_rust import YantrikDB
import yantrikdb
db = YantrikDB(r"{db}", embedding_dim=64, encryption_key={KEY_EXPR})
for i in range({rows}):
    db.record("{CANARY}_" + str(i), memory_type="semantic", namespace="m")
time.sleep({WRITE_DRAIN})
print(yantrikdb.__version__)
''', work)

    before = _scan(work)
    if not before:
        print(json.dumps({
            "written_by": pre_ver,
            "verdict": "INCONCLUSIVE — the control failed",
            "detail": ("the pre-fix engine left no plaintext, so this scan "
                       "cannot detect the freed-page case. Either the writer "
                       "is already fixed, or the fixture is wrong. A clean "
                       "result below would mean nothing."),
        }, indent=2))
        return 2

    t0 = time.time()
    out = _run(candidate_py, f'''
import time
from yantrikdb._yantrikdb_rust import YantrikDB
import yantrikdb
db = YantrikDB(r"{db}", embedding_dim=64, encryption_key={KEY_EXPR})
time.sleep({MIGRATE_DRAIN})
print(yantrikdb.__version__, db.is_encrypted, db.oplog_plaintext_rows())
''', work)
    elapsed = time.time() - t0
    after = _scan(work)
    cand_ver, encrypted, counted = out.split()

    print(json.dumps({
        "rows": rows,
        "written_by": pre_ver,
        "migrated_by": cand_ver,
        "migration_wall_s": round(elapsed, 1),
        "canary_before_migration": before,
        "is_encrypted_after": encrypted,
        "oplog_plaintext_rows_after": counted,
        "canary_after_migration": after,
        "verdict": ("RESIDUAL PLAINTEXT IN FREED PAGES — every live row is "
                    "sealed and the counter reads 0, but the bytes remain"
                    if after else
                    "clean — the migration erased the pages it vacated"),
        "note": ("oplog_plaintext_rows counts ROWS, NOT BYTES, and returns 0 "
                 "on an affected file. It cannot detect this. The byte scan "
                 "is the only sufficient verification."),
    }, indent=2))
    return 1 if after else 0


if __name__ == "__main__":
    raise SystemExit(main())
