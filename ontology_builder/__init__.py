"""AI-Powered Plant Ontology Builder.

Stage 0 (ingest) + Stage 1 (entity resolution) implementation.

Pure standard-library implementation so it runs with no extra installs.
Similarity/classification logic is isolated behind `llm_provider.py` so a
real embeddings/LLM call can be swapped in later without touching the
resolution logic.
"""
