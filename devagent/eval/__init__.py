# devagent/eval/ — the A/B eval harness (M5).
"""Drives the Executor seam across a fixture corpus to answer the two empirical questions the
seam exists to settle: which build arm produces better apps, and what each costs.

Per fixture: freeze scope+plan ONCE, then build each arm N times from those identical bytes,
score each run (deterministic acceptance + a blinded LLM judge + dual-normalized cost), and
aggregate. The LLM and Docker touchpoints are injected so the harness unit-tests without either."""
