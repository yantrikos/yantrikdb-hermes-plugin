"""At what k does the instability begin? Wide k is clean, small k is not — the
crossover localises the defect to a window size rather than a code path guess."""
import gc
import json
import shutil
import sys
import tempfile
from pathlib import Path

base = Path(sys.argv[1])
from yantrikdb._yantrikdb_rust import YantrikDB

Q = "What is Taylor's role?"
KS = [5, 8, 10, 12, 15, 20, 25, 30, 40, 60]
tmproot = Path(tempfile.mkdtemp()); out = {}
for ki, k in enumerate(KS):
    orders, sets_ = [], []
    for i in range(5):
        d = tmproot / f"{ki}_{i}"; d.mkdir(parents=True)
        for f in base.parent.glob(base.name + "*"):
            shutil.copy2(f, d / f.name)
        db = YantrikDB.with_default(str(d / base.name))
        res = [(h.get("text") or "").strip() for h in
               db.recall_text(Q, top_k=k, namespace="g")]
        orders.append(tuple(res)); sets_.append(set(res))
        del db; gc.collect()
    common, union = set.intersection(*sets_), set.union(*sets_)
    out[k] = {"distinct_orderings": len(set(orders)),
              "returned": len(union), "churned": len(union - common)}
print(json.dumps(out, indent=2))
shutil.rmtree(tmproot, ignore_errors=True)
