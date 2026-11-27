# Concurrent skill revisions: what is atomic, and what is not

Scope: `EmbeddedYantrikDBClient.skill_define(..., supersedes_skill_id=…)`, i.e. the
write path that turns "v2 replaces v1" into a native `supersedes` link. This note
records what the code guarantees, where it stops guaranteeing it, and what the
remaining failure leaves behind. Numbers are counts from
`benchmarks/supersedes_race.py`, not estimates.

Reproduce everything below:

```
python benchmarks/supersedes_race.py --runs 40
```

## The invariant and where it lives

The engine keeps at most one *selected, active* inbound `supersedes` edge per
predecessor. That is the single-successor invariant, and it is enforced in
`link()`, in `gate_supersedes()` (`crates/yantrikdb-core/src/engine/links.rs`):
a `SELECT` over `record_links`, then the insert, both while the caller holds
`self.conn.lock()` — one `Mutex<Connection>` per engine instance.

The invariant is therefore **per engine handle**, not per database file. The
schema does not back it up: `record_links` has
`UNIQUE(source_rid, target_rid, link_type)`, which stops the same edge twice, but
nothing stops two *different* sources from both being `selected`/`active`
successors of one target. Two handles are two connections with two mutexes; the
`SELECT` of each can run before either `INSERT`, and then both inserts are
legal.

## Measured

One engine, two clients, two threads that both read v1 before either writes:

```
40/40  one winner + typed refusal
       post-state: 1 edge on v1, 1 current version, 2 history rows
```

An engine-level probe (no plugin code, `link()` called concurrently on one
handle) agrees: 60/60 without a barrier and 60/60 with one, always exactly one
accepted edge.

Two engine handles on one file (`share_engine=False`), same two threads:

```
28/40  one winner + typed refusal
12/40  two winners — two selected active supersedes edges on one predecessor
```

In all 12 two-winner runs both writers returned `stored=True`; afterwards the
predecessor had two inbound `supersedes` edges (measured via
`linked_records(v1, direction="inbound")`).

## The fork report is asymmetric

The provider cannot prevent the second write, so it repairs after the fact: a
writer that detects another successor of the same predecessor links its own new
record to that one (`_db.link(rid, other, "supersedes")`) and reports
`concurrent_revisions` + a `warning`. The repair converges — one of the two
records ends up superseded — but only one writer learns about it.

In 12 of 12 two-winner runs exactly one writer carried `concurrent_revisions`;
the other returned a plain success and its own engine still showed its record as
`is_current`. A caller that believes it wrote the current version is the state
to avoid, and it is not currently avoidable across handles.

## Crash semantics of the write

`skill_define` writes in two steps, because the `rid` is needed for the edge:

1. `record_text(...)` — the row is committed;
2. `link(rid, predecessor, "supersedes")` — the engine may refuse here.

The refusal path is covered: the row is removed with `forget()` and the caller
gets `YantrikDBClientError as "revised this skill concurrently"`, so a refusal
leaves neither an orphan row nor a second active version. Two windows remain,
and neither is reachable from Python:

- **Crash between (1) and (2).** SIGKILL, power loss or an interpreter abort
  between the two calls leaves a committed record with no `supersedes` edge. It
  is an active, unlinked duplicate that the fork detection cannot see, because
  fork detection looks for edges. The repair is `skill_reify_revisions` (it links
  a chain by position) or an explicit retirement; nothing detects the orphan on
  its own.
- **`record_with_links` on wheels without `link()`.** That path persists the
  record *before* validating the link, so a refusal leaves the row behind. The
  provider maps the resulting `RuntimeError` to a typed error but cannot undo the
  write; the code only takes that path when `_db.link` is missing.

## Operating envelope

A single engine handle per database file per process is the supported
configuration for skill revisions, and `share_engine=True` (the default) is what
provides it: the cache key is the absolute db path plus the embedder settings,
so every client in the process gets the same handle and therefore the same
connection mutex.

Opening a second handle on the same file — `share_engine=False`, or a second
process — is outside that envelope for concurrent revision writes. It is not
merely a lost race: an already-open handle also does not see records another
handle writes afterwards. In the two-winner reproduction the seed handle
returned only the predecessor from `skill_search` while `linked_records` showed
both new edges; a handle opened afterwards on a quiescent store returned all
rows (2/2, no concurrency). We did not investigate that further — it is recorded
because it decides what "two active versions served by search" can even mean
across handles: a single handle sees one writer at most.

## What the CI pins, and what it does not

`tests/test_provider.py::TestRealConcurrentRevision` asserts the in-engine case:
two clients, one engine, exactly one success, one typed refusal, one current
version. It asserts the engine identity as a premise, so a future change to the
cache cannot silently turn it into a cross-handle test.

The cross-handle case is a reproducible engine finding, not a CI assertion. It
belongs to the engine because the fix does:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS record_links_single_successor
    ON record_links(target_rid, link_type)
    WHERE link_type = 'supersedes'
      AND selection_state = 'selected'
      AND status = 'active';
```

That makes the invariant a property of the database instead of a property of
"everyone holds the same mutex", which also covers a second process and a second
handle. Candidate edges stay possible — the schema already stores losing
candidates as `selection_state = 'rejected_conflict'` — so the index matches the
design intent rather than narrowing it.
