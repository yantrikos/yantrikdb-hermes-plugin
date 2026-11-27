"""End-to-End: provider revision + outcome read path, against a real engine.

Runs the patched provider module from /tmp/plug against a temp store.
Each check prints PASS/FAIL; exit code != 0 if anything failed.
"""
import hashlib
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yantrikdb

PLUGIN = Path(__file__).resolve().parents[2] / "yantrikdb"


class StubEmbedder:
    def encode(self, text: str) -> list[float]:
        h = hashlib.sha256((text or "").encode()).digest()
        return [((h[i % len(h)] / 255.0) - 0.5) for i in range(64)]


spec = importlib.util.spec_from_file_location(
    "plug_embedded", PLUGIN / "embedded.py",
    submodule_search_locations=[str(PLUGIN)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules["plug_embedded"] = mod
spec.loader.exec_module(mod)

db_path = str(Path(tempfile.mkdtemp()) / "e2e.yantrik")
engine = yantrikdb.YantrikDB(db_path, 64)
engine.set_embedder(StubEmbedder())

client = mod.EmbeddedYantrikDBClient.__new__(mod.EmbeddedYantrikDBClient)
client._db = engine
client.config = SimpleNamespace(top_k=10)

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


SID = "review.adversarial_validation"
BODY1 = "Check the code first, then try to disprove the finding using the listed counter-probes."
BODY2 = "Check the code first, then try to disprove the finding using the listed counter-probes, and state the counter-probe explicitly."

# 1. first version
r1 = client.skill_define(SID, BODY1, "procedure", ["review"])
check("v1 stored", r1.get("stored") is True and "supersedes" not in r1, str(r1))

# 2. second version -> real supersedes link
r2 = client.skill_define(SID, BODY2, "procedure", ["review"], supersedes_skill_id=SID)
check("v2 supersedes v1", r2.get("supersedes") == r1["rid"], str(r2))

# 3. default search hides the predecessor
cur = client.skill_search("disprove the finding")
rids = [s["rid"] for s in cur["skills"]]
check("search returns only current", r2["rid"] in rids and r1["rid"] not in rids, str(rids))

# 4. history search shows both, marked
hist = client.skill_search("disprove the finding", include_history=True)
by_rid = {s["rid"]: s for s in hist["skills"]}
check("history has both", r1["rid"] in by_rid and r2["rid"] in by_rid, str(list(by_rid)))
check("history marks superseded", by_rid.get(r1["rid"], {}).get("is_current") is False,
      str(by_rid.get(r1["rid"], {}).get("is_current")))

# 5. idempotent rewrite of the same version
again = client.skill_define(SID, BODY2, "procedure", ["review"])
check("same version is unchanged", again.get("unchanged") is True and again["rid"] == r2["rid"],
      str(again))

# 6. same body, different preconditions -> new version (identity is not the body)
r3 = client.skill_define(SID, BODY2, "procedure", ["review"],
                         supersedes_skill_id=SID, preconditions=["needs a pinned revision"])
check("new preconditions create a version", r3.get("stored") is True and r3["rid"] != r2["rid"],
      str(r3))

# 7. chain A -> B -> C is walkable
chain = engine.linked_records(r3["rid"], direction="outbound", link_type="supersedes")
check("chain C->B present", any(x.get("rid") == r2["rid"] for x in chain), str(chain))
chain_b = engine.linked_records(r2["rid"], direction="outbound", link_type="supersedes")
check("chain B->A present", any(x.get("rid") == r1["rid"] for x in chain_b), str(chain_b))

# 8. outcome with the three phases and evidence
o1 = client.skill_outcome(SID, True, phase="execution", note="ran",
                          task="validate api auth", skill_version_rid=r3["rid"])
o2 = client.skill_outcome(SID, True, phase="outcome", note="verdict held",
                          task="validate api auth", evidence_ref="config.yaml:52",
                          skill_version_rid=r3["rid"])
check("outcomes recorded", o1.get("recorded") and o2.get("recorded"), f"{o1} {o2}")

try:
    client.skill_outcome(SID, True, phase="nonsense")
    check("invalid phase rejected", False)
except Exception:
    check("invalid phase rejected", True)

# 9. read by skill_id
by_id = client.skill_outcomes(skill_id=SID, limit=10)
check("outcomes by skill_id", by_id["total"] >= 2, str(by_id["total"]))
check("phase preserved", any(o["phase"] == "outcome" for o in by_id["outcomes"]),
      str([o["phase"] for o in by_id["outcomes"]]))

# 10. situational read must DISCRIMINATE, not just return everything.
# Three outcomes with different tasks; the query must rank the matching one first,
# and a nonsense query must not.
client.skill_outcome(SID, True, phase="outcome", task="rotate TLS certificates",
                     note="cert rotation went fine")
client.skill_outcome(SID, True, phase="outcome", task="migrate a sqlite schema",
                     note="schema migration needed a backup first")
hit = client.skill_outcomes(query="how do I check whether an API enforces authentication", limit=3)
top = hit["outcomes"][0] if hit["outcomes"] else {}
check("situational read ranks the matching task first",
      (top.get("task") or "").startswith("validate api auth"), str(top.get("task")))
noise = client.skill_outcomes(query="quantum chromodynamics lattice spacing", limit=3)
ntop = noise["outcomes"][0] if noise["outcomes"] else {}
check("nonsense query does not rank the target first",
      not (ntop.get("task") or "").startswith("validate api auth"), str(ntop.get("task")))
check("task text is in the indexed body",
      "task:" in (engine.recall(query="validate api auth", top_k=5,
                                namespace=mod.OUTCOME_NAMESPACE) or [{}])[0].get("text", "")
      or any("task:" in (h.get("text") or "") for h in engine.recall(
          query="validate api auth", top_k=5, namespace=mod.OUTCOME_NAMESPACE)),
      "body lacks task")

# 10b. a supersedes id for a different skill is an error, not a silent flag
try:
    client.skill_define(SID, BODY2, "procedure", ["review"],
                        supersedes_skill_id="some.other.skill")
    check("foreign supersedes id rejected", False)
except Exception:
    check("foreign supersedes id rejected", True)

# 10c. no orphan: exactly one version is live after revisions
live = [s for s in client.skill_search("the finding", include_history=True)["skills"]
        if s.get("is_current")]
check("exactly one active version", len(live) == 1, str(len(live)))

# 11. legacy record: metadata.supersedes_skill_id but no link -> reified positionally
legacy_rid = engine.record_text(
    "old guidance without a link", memory_type="procedural",
    namespace=mod.SKILL_NAMESPACE, domain="skill",
    metadata={"record_type": "skill", "skill_id": SID, "skill_type": "procedure",
              "supersedes_skill_id": SID},
)
before = engine.recall(query=BODY2, top_k=10, namespace=mod.SKILL_NAMESPACE)
reify = client.skill_reify_revisions(SID)
after = engine.recall(query=BODY2, top_k=10, namespace=mod.SKILL_NAMESPACE)
check("legacy chain reified", reify.get("ok") is True and reify.get("links_created", 0) >= 1,
      str(reify))
check("older version still retrievable as history",
      any(h["rid"] == legacy_rid for h in engine.recall(
          query="old guidance", top_k=10, namespace=mod.SKILL_NAMESPACE,
          include_superseded=True)),
      "history read failed")


# 12. NEW SESSION: same store, fresh engine + client. The acceptance criterion.
fresh_engine = yantrikdb.YantrikDB(db_path, 64)
fresh_engine.set_embedder(StubEmbedder())
fresh = mod.EmbeddedYantrikDBClient.__new__(mod.EmbeddedYantrikDBClient)
fresh._db = fresh_engine
fresh.config = SimpleNamespace(top_k=10)

found_by_task = fresh.skill_outcomes(
    query="I must check whether a third-party API enforces authentication", limit=10)
check("new session: outcome found by task",
      any(o["skill_id"] == SID for o in found_by_task["outcomes"]),
      str(found_by_task["total"]))

found_skill = fresh.skill_search("disprove the finding")
cur_rids = [s["rid"] for s in found_skill["skills"]]
check("new session: only current version served",
      r3["rid"] in cur_rids and r1["rid"] not in cur_rids and r2["rid"] not in cur_rids,
      str(cur_rids))

hist2 = fresh.skill_search("disprove the finding", include_history=True)
check("new session: history walkable",
      len({s["rid"] for s in hist2["skills"]} & {r1["rid"], r2["rid"], r3["rid"]}) >= 3,
      str([s["rid"][:8] for s in hist2["skills"]]))

spec2 = importlib.util.spec_from_file_location(
    "plug_init", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
m2 = importlib.util.module_from_spec(spec2)
sys.modules["plug_init"] = m2
spec2.loader.exec_module(m2)
names = {s["name"] for s in m2.ALL_TOOL_SCHEMAS}
check("skill_outcomes tool exposed", "yantrikdb_skill_outcomes" in names, str(sorted(n for n in names if "skill" in n)))
check("skill_reify tool exposed", "yantrikdb_skill_reify" in names)
props = m2.SKILL_OUTCOME_SCHEMA["parameters"]["properties"]
check("outcome schema carries task/phase/evidence",
      {"task", "phase", "evidence_ref", "skill_version_rid", "corrects_rid"} <= set(props),
      str(sorted(props)))
check("define schema carries preconditions",
      "preconditions" in m2.SKILL_DEFINE_SCHEMA["parameters"]["properties"])

print("\nFAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
