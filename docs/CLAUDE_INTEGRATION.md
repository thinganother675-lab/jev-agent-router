# Claude Code integration

## A. This project's SIMPLE shadow router

`hooks/jev_shadow_hook.py` is a project-scope `UserPromptSubmit` hook. It reads the hook JSON as UTF-8, sends the prompt to the local sidecar and records two independent shadow predictions: skill/complexity via `/decide`, and agent work shape via `/agent_decide`. It emits no stdout decision or context and exits successfully on errors. The hook is meant to be registered asynchronously in a local `.claude/settings.json` (ignored by Git) after the sidecar is running.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /opt/jev-agent-router/hooks/jev_shadow_hook.py",
            "async": true,
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

Adjust the absolute path and verify the installed Claude Code hook schema. Place the setting in the intended project, not in this repository template. Set `JEV_SHADOW_LOG` and `JEV_AGENT_SHADOW_LOG` to private, **distinct** per-consumer paths. Keep `JEV_SHADOW_LOG_PROMPTS` unset. The optional collector is off by default. `JEV_AGENT_SHADOW=0` disables the second prediction. The sidecar and hook both fail open; a down sidecar should not block Claude.

## B. Upstream `jev-skill-suggestion`

The upstream cookbook-style integration makes a first choice, then conditionally reranks candidates with a second Jev call. It is useful prior art, but its defaults were not the selected policy for this catalog. Review the upstream implementation and its timeout, catalog descriptions and trust model before trying it with your installed Claude version.

On **this project's catalog/test only**, SIMPLE scored about 95% on the holdout. The upstream-inspired official-default variant scored materially lower; tuned official and hybrid variants were statistically close to SIMPLE. A second call did not show a clear net benefit. Skill descriptions had a large effect, and the trigger-shaped rewrite still needs a fresh holdout. These results do **not** say SIMPLE is better than the official integration in general. See [benchmarks](BENCHMARKS.md).

Active skill injection is intentionally disabled. A decision to enable it needs privacy review, new holdout validation and a local policy that handles errors and confidence conservatively.
