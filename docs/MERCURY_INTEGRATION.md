# Mercury as a worker

Mercury 2.5 was tested as an alternative router and as a worker. It was **not selected as the main router** in this project: routing outputs varied more between runs and had a longer latency tail in the local benchmark. That finding is specific to the tested catalog and cases. Jev remains the decision layer; Mercury is a candidate for bounded generation work.

Good worker tasks include context compaction, structured extraction, summarizing logs, and a shortlist of files or tools for a human/agent to inspect. A Mercury recommendation from `/agent_decide` is only a candidate; local policy still chooses whether to call Mercury. `src/mercury_client.py` uses the standard library and accepts `mercury_api`, `INCEPTION_API_KEY`, or `MERCURY_API_KEY`. On a new VPS use the last name in a private environment file. Mercury is optional for running Jev.

## Schema-shaped compaction

The compaction tests favored a prompt that names the exact output fields and preservation rules. For example:

```text
Return JSON with: objective, decisions, constraints, verified_facts,
open_questions, next_actions. Preserve exact file names, commands and
unresolved errors when supplied. Do not invent missing facts. Mark unknowns.
Keep the result concise and valid JSON only.
```

Validate the JSON shape locally; do not treat the worker output as ground truth. Do not send secrets or raw private transcripts without a deliberate privacy decision. Keep input length bounded, redact first, and test whether the compacted result preserves the facts the next agent needs. The benchmark found useful compaction behavior from schema-shaped prompts, while omission remained a risk. See [benchmarks](BENCHMARKS.md).
