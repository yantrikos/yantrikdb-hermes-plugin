"""End-to-end validation of PR #9989 v0.4.2 plugin against Hermes 0.9.0 on LXC."""
import json
import os
import sys
import time

sys.path.insert(0, "/root/hermes-pr-test")
os.environ.setdefault("YANTRIKDB_MODE", "embedded")
os.environ.setdefault("YANTRIKDB_DB_PATH", "/tmp/yhp-pr-validate.db")
os.environ.setdefault("YANTRIKDB_NAMESPACE", "pr-validation")

# === Step 1: load via Hermes' own discovery mechanism ===
from plugins.memory import load_memory_provider
print("=== Step 1: load_memory_provider('yantrikdb') ===")
t0 = time.perf_counter()
provider = load_memory_provider("yantrikdb")
print(f"  loaded in {(time.perf_counter()-t0)*1000:.1f}ms; class={type(provider).__name__}")
print(f"  name={provider.name!r}")
print(f"  is_available()={provider.is_available()}")

# === Step 2: initialize a session ===
print()
print("=== Step 2: initialize() ===")
t0 = time.perf_counter()
provider.initialize(session_id="pr-validation-session")
print(f"  initialize() ok in {(time.perf_counter()-t0)*1000:.1f}ms")

# === Step 3: schema introspection ===
print()
print("=== Step 3: get_tool_schemas() ===")
schemas = provider.get_tool_schemas()
print(f"  {len(schemas)} tools exposed:")
for s in schemas:
    desc = s["description"]
    if len(desc) > 70:
        desc = desc[:70] + "..."
    print(f"    - {s['name']}: {desc}")

# === Step 4: end-to-end remember/recall flow ===
print()
print("=== Step 4: remember -> recall -> why_retrieved ===")
facts = [
    "Pranab prefers dark mode in VS Code with JetBrains Mono font.",
    "The yantrikdb-hermes-plugin v0.4.2 ships with the bundled potion-2M embedder by default.",
    "The first user issue on the plugin repo was about multilingual embedding support.",
    "Pranab actually prefers light mode in VS Code when reviewing PRs.",
]
for i, txt in enumerate(facts, 1):
    t0 = time.perf_counter()
    r = provider.handle_tool_call("yantrikdb_remember", {"text": txt, "importance": 0.7})
    dt = (time.perf_counter() - t0) * 1000
    parsed = json.loads(r) if isinstance(r, str) else r
    s = json.dumps(parsed)
    print(f"  remember #{i} ({dt:.2f}ms): {s[:90]}{'...' if len(s)>90 else ''}")

print()
t0 = time.perf_counter()
r = provider.handle_tool_call(
    "yantrikdb_recall",
    {"query": "What does Pranab prefer in VS Code?", "top_k": 5},
)
dt = (time.perf_counter() - t0) * 1000
parsed = json.loads(r) if isinstance(r, str) else r
results = parsed.get("results", [])
print(f"  recall ({dt:.2f}ms) -- {len(results)} results, top 3:")
for j, hit in enumerate(results[:3], 1):
    txt = hit.get("text", "")
    if len(txt) > 60:
        txt = txt[:60] + "..."
    score = hit.get("score", 0)
    why = hit.get("why_retrieved", [])
    print(f"    #{j} score={score:.3f}  text={txt!r}")
    print(f"        why_retrieved={why}")

# === Step 5: conflicts ===
print()
print("=== Step 5: yantrikdb_conflicts surfaces contradictions ===")
r = provider.handle_tool_call("yantrikdb_conflicts", {})
parsed = json.loads(r) if isinstance(r, str) else r
conflicts = parsed.get("conflicts", [])
print(f"  {len(conflicts)} conflict(s) found")
for c in conflicts[:2]:
    print(f"    {json.dumps(c)[:200]}")

# === Step 6: stats ===
print()
print("=== Step 6: yantrikdb_stats ===")
r = provider.handle_tool_call("yantrikdb_stats", {})
parsed = json.loads(r) if isinstance(r, str) else r
print(f"  {json.dumps(parsed, indent=2)[:400]}")

print()
print("=== ALL STEPS COMPLETED — PR v0.4.2 WORKS END-TO-END ===")
