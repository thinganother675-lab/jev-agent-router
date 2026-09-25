# Benchmarks and research status

The selected skill router is SIMPLE: a single Jev choice including `none`, plus complexity classification. These findings come from **this project's catalog and test set**, not a general comparison of Jev integrations.

The 2026-09-23 architecture study used 226 labeled cases (79 development, 147 held out), with three final runs on the test split. Acceptable accuracy in the first test run was SIMPLE **95.2%**, upstream-inspired OFFICIAL-DEFAULT **84.1%**, development-tuned OFFICIAL **92.4%**, and HYBRID **95.2%**. SIMPLE, tuned OFFICIAL and HYBRID were statistically close in paired analysis. OFFICIAL-DEFAULT was materially worse on this catalog. A second Jev call showed no clear accuracy benefit here while adding latency and tokens.

Catalog descriptions matter strongly. Trigger-shaped descriptions reduced false positives in a development experiment, but that edited catalog still needs a fresh holdout; it was not promoted to production. The older 15-case experiment was a development set and does not supersede the larger study. Jev was near-deterministic over repeated runs, not perfectly deterministic.

Mercury 2.5 was compared on a separate blind 50-case exercise. Jev's scored decisions were 46/46 in three runs; Mercury's were 45, 43 and 44. This small difference does not establish a broad model ranking. Mercury showed more run-to-run variation and a longer routing latency tail in this setup; its useful role is structured worker work, especially context compaction. See [Mercury integration](MERCURY_INTEGRATION.md).

The repository preserves the architecture, code and documented aggregate findings. Raw per-case results, private prompts, real-session telemetry and generated datasets remain local and excluded from Git. Any fresh benchmark must rebuild a consented dataset, keep labels separate from inference, freeze a holdout before tuning, and report uncertainty and costs.
