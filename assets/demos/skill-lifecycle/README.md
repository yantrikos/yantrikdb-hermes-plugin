# Skill Lifecycle Demo

End-to-end demo showing the [`yantrikdb-hermes-plugin`](https://github.com/yantrikos/yantrikdb-hermes-plugin) skill substrate handling the **define → restart → search → outcome** loop — the autonomy loop described in [`yantrikdb/README.md`](../../../yantrikdb/README.md) and on [`yantrikdb.com/guides/autonomous-skills/`](https://yantrikdb.com/guides/autonomous-skills/).

**Captured output:** [`transcript.txt`](./transcript.txt) — the demo's stdout from a real run against an ephemeral SQLite substrate, ~25 seconds end-to-end.

**Animated GIF**: a [`demo.tape`](./demo.tape) file is included for [`vhs`](https://github.com/charmbracelet/vhs) rendering; the Windows VHS path has a shell-detection issue that needs follow-up (the tape script renders cleanly on macOS/Linux). Until that's fixed, the transcript above is the canonical captured output.

## What's shown

| Step | What the plugin does | What you see |
|---|---|---|
| 1 | Fresh ephemeral substrate, 0 skills | `yantrikdb_stats` returns zero memories, zero operations |
| 2 | Agent observes a repeated pattern, calls `yantrikdb_skill_define` for a release-workflow procedure | rid returned, `stored: true`, substrate operations count ticks up |
| 3 | Simulated session restart — provider torn down + a fresh instance created | new `YantrikDBMemoryProvider()` instance, same SQLite file underneath |
| 4 | Session 2's agent calls `yantrikdb_skill_search("how to ship a release")` | the skill from session 1 returned, ranked by relevance |
| 5 | Agent follows the procedure, calls `yantrikdb_skill_outcome(succeeded=True, note=…)` | rid returned, `recorded: true`, outcome ledger appended |

The demo runs in ~25 seconds against a fresh ephemeral SQLite DB. No LLM in the loop. The LLM-driven part — "agent decides to call skill_define / skill_search" — is scripted here so the recording is deterministic; everything below that line (the plugin's `handle_tool_call` dispatch, the engine's `yantrikdb` Rust core, the SQLite writes, the embedding+search, the response shapes) is live code.

## What this is and isn't

**This is**: the same `handle_tool_call` entry point Hermes uses to invoke yantrikdb tools when its agent's LLM emits a tool call. The plugin code is real ([`yantrikdb_hermes_plugin.YantrikDBMemoryProvider`](../../../yantrikdb/__init__.py)). The engine is real (`yantrikdb` on PyPI). The substrate is real (SQLite under the temp dir).

**This is not**: a recording of Claude / GPT / Qwen deciding to call these tools in response to a natural-language prompt. That part is scripted for the recording to run cleanly in <30s — the demo proves the plugin's plumbing, not the LLM's autonomy.

For evidence of LLM-driven autonomy, see [`yantrikdb.com/guides/autonomous-skills/`](https://yantrikdb.com/guides/autonomous-skills/), which documents 17 skills authored by Claude (via the `yantrikdb-mcp` server) on one production substrate, with 9 of them showing cross-session reuse via the outcome ledger.

## Reproduce

```bash
# In any environment where yantrikdb-hermes-plugin is installed:
pip install yantrikdb-hermes-plugin yantrikdb

# Run the demo (Windows shown — on POSIX, just use `python`):
python assets/demos/skill-lifecycle/demo.py
```

The script creates a fresh temp dir for `YANTRIKDB_DB_PATH`, walks through the five steps, and prints what each `handle_tool_call` returns. Output is structured JSON from the plugin (truncated in the recording for readability).

## Re-rendering the GIF

Install [`vhs`](https://github.com/charmbracelet/vhs) — `winget install charmbracelet.vhs` on Windows, or follow the repo for macOS/Linux. Then:

```bash
cd assets/demos/skill-lifecycle
vhs demo.tape
```

Outputs `demo.gif` (~800 KB, embeddable in READMEs) and `demo.mp4`.
