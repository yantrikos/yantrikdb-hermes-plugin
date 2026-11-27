# Consolidation and recall: what we measured on a live store

Findings from a 23k-record store (3,867 summaries, 15,000 membership rows) running
YantrikDB 0.23.6 through this plugin. Every number was measured on a **copy**; the
production database was opened read-only. Filed upstream as issues #291 (carrier of the
recall loss), #110 (contradiction links never surface) and #88 (`forget()` does not cascade
to `consolidation_members`).

This page exists so the measurements sit next to the code they describe, not only in an
issue thread.

## 1. A consolidated source is not ranked lower — it is not a candidate

Nine members written by a `consolidate()` run, three query shapes (full text, a
200-character window from the middle of the member, its five longest non-stopword terms),
20 hits each:

| query | default recall | `include_consolidated=True` |
|---|---|---|
| full text | member **0/9**, summary 9/9 | member 9/9, ranks 2-3 |
| 200-char window from the middle | **0/9** | 8/9, ranks 2-20 |
| five keywords | **0/9** | **3/9**, ranks 3, 4, 17 |

Before the run the same queries returned the member at rank 1 in 8/9 cases. The loss is
removal, not degradation — and a verbatim 200-character window from the middle does not
bring the record back.

## 2. The carrier is the eligibility filter, not the 0.3x importance cut

Four arms on copies, same nine members, same queries. "restored" means the value was set
back to its pre-consolidation state **on a copy**.

| arm | full text: found / rank 1 | keywords: found / rank 1 |
|---|---|---|
| status `consolidated`, importance 0.3x, `include_consolidated=True` | 9/9, **0x** | 3/9, 0x |
| importance restored only | 9/9, **2x** | 5/9, 0x |
| status restored to `active` only (importance still 0.3x) | 9/9, **9x** | 9/9, **7x** |

Restoring only the status recovers rank 1 in every full-text case while the importance
stays at 0.3x. The importance cut is real but secondary.

## 3. The summary vector is the exact arithmetic mean, unnormalised

The engine's own doc comment says the default path carries "the MEAN of their embeddings".
Verified numerically on six of the forty summaries written by our run:

- `cos(summary_vector, mean(member_vectors))` = **1.000000**
- largest per-component difference: **0.0**
- `||summary_vector||` = 0.837 - 0.905, so the mean is stored **without** L2 normalisation

That is why the summary wins on full text (it is the cluster centroid) and loses on
keywords (it is close to nothing specific).

## 4. What a run does, as a baseline

`dry_run` predicted **40 clusters / 153 members**; the run produced exactly that, twice, on
two independent copies (identical: 3,907 summaries, 15,153 membership rows).

- RIDs lost: 0. Texts changed: 0. Dangling `consolidated_from` entries: 0. Cross-namespace
  clusters: 0 (source confirms: `consolidate()` buckets by `namespace` before clustering, and
  `consolidate_cluster`/`record_synthesis` refuse a cross-namespace input).
- Importance after the run: exactly **0.3000** for all 153 (min = max = 0.3).
- Oplog delta by `op_type`: `consolidate` 40, `materialize_record_post` 40, `record` 40. No
  op type appeared that was not already present.
- Runtime 0.61 s / 0.67 s; `limit=100` did not bind.

## 5. Why the recall payload now carries `namespace`, `current_status`, `superseded_by`

A hit already carries all three (measured on the 0.23.6 wheel: `namespace`, `current_status`,
`superseded_by`, plus `metadata.consolidated_from` with the source rids); the plugin's compact
result dropped them. They are now passed through unchanged — no lookup, no default. A field the
engine does not return stays `None` rather than being reported as `"active"`.

`consolidation_status` is deliberately absent: this plugin never asks for consolidated records,
so the value could not vary. It belongs here once the plugin can request them.

The engine-side numbers behind this change are in yantrikos/yantrikdb#291.

## Limits

Nine members for the arms, one store, one embedder; the mean was verified on six of forty
summaries. Nothing here measures the **additive** mode's quality — no caller exists for it in
0.23.6, and that gap is the subject of #291, not of this change.
