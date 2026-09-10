"""Cross-layer machinery with zero internal dependencies (stdlib only):
approval gate, interrupts, log redaction, tool-result side-store, path
rewriting, process utils, sanitized subprocess env. Everything here may be
imported at module level from anywhere — it is the bottom of the DAG."""
