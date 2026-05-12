# yantrikdb — 1000-memory scale probe
started: 2026-05-12 18:07:03 UTC
tools: ['yantrikdb_remember', 'yantrikdb_recall', 'yantrikdb_forget', 'yantrikdb_think', 'yantrikdb_conflicts', 'yantrikdb_resolve_conflict', 'yantrikdb_relate', 'yantrikdb_stats']
writing 1000 facts via yantrikdb_remember...
write retries exhausted for fact #257 after 60 attempts
write retries exhausted for fact #258 after 60 attempts
write retries exhausted for fact #259 after 60 attempts
WRITE TIMEOUT at fact #357 after 600.0s
backpressure retries: 6000
writes: 256/1000 ok (failures=100); p50=0.48ms p99=5.13ms
running 20 queries via yantrikdb_recall...
recalls: 20/20 ok; p50=3.78ms p99=32.94ms; precision@K=16/20
shape (first non-empty result): why_retrieved=True('why_retrieved') score=True metadata=False
duplicate-canonicalization: avg results per Q-dup-* query = 5.0 → false
