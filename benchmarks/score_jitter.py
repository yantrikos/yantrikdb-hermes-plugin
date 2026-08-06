"""Per-query jitter breakdown. My 'candidate generation is clean' claim rested
on two queries; check whether it holds across more."""
import gc
import json
import shutil
import sys
import tempfile
from pathlib import Path

base = Path(sys.argv[1])
from yantrikdb._yantrikdb_rust import YantrikDB

QS = ["What is Taylor's role?", "What is Bob's role?", "taylor",
      "What is Dave's role?", "who reports to Grace"]
tmproot = Path(tempfile.mkdtemp()); out = {}
for qi, q in enumerate(QS):
    runs = []
    for i in range(5):
        d = tmproot / f"{qi}_{i}"; d.mkdir(parents=True)
        for f in base.parent.glob(base.name + "*"):
            shutil.copy2(f, d / f.name)
        db = YantrikDB.with_default(str(d / base.name))
        runs.append({(h.get("text") or "").strip(): h.get("score", 0.0)
                     for h in db.recall_text(q, top_k=60, namespace="g")})
        del db; gc.collect()
    sets = [set(r) for r in runs]
    common, union = set.intersection(*sets), set.union(*sets)
    ds = sorted((max(r[t] for r in runs) - min(r[t] for r in runs)) for t in common)
    out[q] = {"returned": len(union), "stable_membership": len(common),
              "churned": len(union - common),
              "max_jitter": f"{ds[-1]:.3e}", "median_jitter": f"{ds[len(ds)//2]:.3e}",
              "count_gt_1e-5": sum(1 for d in ds if d > 1e-5)}
print(json.dumps(out, indent=2))
shutil.rmtree(tmproot, ignore_errors=True)
