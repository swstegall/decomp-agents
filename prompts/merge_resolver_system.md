# Merge resolver system prompt (reserved)

Not used by v1. Reserved for a future enhancement where an LLM-backed
resolver tries non-trivial merge conflicts before escalating.

For now, the merge orchestrator (src/decomp_agents/merge.py) only
auto-resolves YAML-only row conflicts and escalates everything else by
writing a JSON to output/merges/escalated/.
