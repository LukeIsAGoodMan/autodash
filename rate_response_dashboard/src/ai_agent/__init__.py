"""AI report generator. Reads the existing rollup + decile marts and emits
a structured analytical report. Stage 1 is fully deterministic (no LLM).

Pipeline:
    snapshot_builder  →  ReportFacts
    mom_yoy           →  MoMYoYAnalysis    (consumes ReportFacts)
    model_compare     →  ModelComparison   (consumes ReportFacts + decile marts)
    renderer          →  HTML string       (consumes ReportPackage)

Add LLM-driven modules in Stage 2+ behind the same fact interface.
"""
