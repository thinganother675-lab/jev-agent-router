"""Build the router-architecture benchmark: 223 synthetic cases + real-world cases, split dev/test.

    python benchmarks/build_router_dataset.py            # writes the jsonl files, once
    python benchmarks/build_router_dataset.py --force    # only before any test run exists

This file is the one place where prompts and labels live together. It writes them apart:

    benchmarks/router_{dev,test}_prompts.jsonl   id + prompt only   <- inference reads these
    benchmarks/router_{dev,test}_labels.jsonl    id + labels only   <- only router_scoring reads these
    benchmarks/private/router_real_prompts.jsonl real prompts, git-ignored, never git-ready

The labels were written on 2026-09-23 **before any variant was run on these prompts**, by one
labeller (Claude), against catalog simple_v1 (src/skills_catalog.json, 36 skills). "Expected"
means the one skill that should be loaded before working on the prompt; None means Claude
should proceed without a skill. `acceptable` lists other answers a reasonable engineer would
not call wrong (None inside it means abstaining is also fine). Cases a human could not settle
either are flagged `ambiguous` and scored separately, not as ordinary binary errors.

Split: stratified by group, fixed seed, ~35% dev / ~65% test. Real-world: one case in dev,
the rest in test, so the final test always contains real prompts.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRIVATE = HERE / "private"
SEED = 20260923
DEV_FRACTION = 0.35

N = None  # readability: "no skill"

# (id, prompt, expected, acceptable, difficulty, category, tags, adversarial, ambiguous, lang, rationale)
# group is the id prefix: N no-skill, S one-skill, K keyword-trap, E topic-not-needed,
# I implicit-need, D similar-skills, C compound, A mandated adversarial.
CASES = [
    # ---------------------------------------------------------------- N: skill clearly not needed (50)
    ("N001", "Rename the variable `tmp` to `retryCount` in utils.py", N, [], "trivial", "normal", [], False, False, "en", "plain edit"),
    ("N002", "Why does my Python script throw KeyError: 'user_id' on line 42?", N, [], "trivial", "normal", [], False, False, "en", "plain debugging"),
    ("N003", "Add type hints to the functions in parser.py", N, [], "trivial", "normal", [], False, False, "en", "plain edit"),
    ("N004", "What's the difference between a process and a thread?", N, [], "trivial", "normal", [], False, False, "en", "general knowledge"),
    ("N005", "Write a unit test for the slugify function", N, [], "trivial", "normal", [], False, False, "en", "plain coding"),
    ("N006", "The login endpoint returns 500 when the email has a plus sign, can you find out why?", N, [], "normal", "normal", [], False, False, "en", "debugging"),
    ("N007", "Convert this callback-based function to async/await", N, [], "trivial", "normal", [], False, False, "en", "refactor"),
    ("N008", "How do I undo my last git commit but keep the changes?", N, [], "trivial", "normal", [], False, False, "en", "git question"),
    ("N009", "Refactor the OrderService class so the payment logic lives in its own module", N, [], "normal", "normal", [], False, False, "en", "refactor"),
    ("N010", "Add pagination to the /api/products endpoint", N, [], "normal", "normal", [], False, False, "en", "feature"),
    ("N011", "Explain what this SQL query does: SELECT user_id, COUNT(*) FROM orders GROUP BY user_id HAVING COUNT(*) > 5", N, [], "trivial", "normal", [], False, False, "en", "explanation"),
    ("N012", "Bump the lodash version in package.json and fix any breaking changes", N, [], "normal", "normal", [], False, False, "en", "dependency upgrade"),
    ("N013", "Write a bash script that renames all .jpeg files in a folder to .jpg", N, [], "trivial", "normal", [], False, False, "en", "scripting"),
    ("N014", "Why is my React component re-rendering on every keystroke?", N, [], "normal", "normal", [], False, False, "en", "debugging"),
    ("N015", "Make the CLI accept a --verbose flag", N, [], "trivial", "normal", [], False, False, "en", "feature"),
    ("N016", "Implement an LRU cache in TypeScript with get and put in O(1)", N, [], "normal", "normal", [], False, False, "en", "coding"),
    ("N017", "Fix the flaky test in test_payments.py, it fails about one run in five", N, [], "normal", "normal", [], False, False, "en", "debugging"),
    ("N018", "What does the `yield` keyword do in Python?", N, [], "trivial", "normal", [], False, False, "en", "general knowledge"),
    ("N019", "Add a Dockerfile for this Flask app", N, [], "trivial", "normal", [], False, False, "en", "devops edit"),
    ("N020", "Our CI build takes 25 minutes, suggest ways to speed it up", N, [], "normal", "normal", [], False, False, "en", "advice"),
    ("N021", "Write a regex that matches Russian mobile phone numbers", N, [], "trivial", "normal", [], False, False, "en", "coding"),
    ("N022", "Migrate this component from a class component to hooks", N, [], "normal", "normal", [], False, False, "en", "refactor"),
    ("N023", "Why does 0.1 + 0.2 not equal 0.3 in JavaScript?", N, [], "trivial", "normal", [], False, False, "en", "general knowledge"),
    ("N024", "Add input validation to the signup form: email format and a password of at least 12 characters", N, [], "trivial", "normal", [], False, False, "en", "feature"),
    ("N025", "Implement retry with exponential backoff for the HTTP client", N, [], "normal", "normal", [], False, False, "en", "coding"),
    ("N026", "Delete the unused imports across the src folder", N, [], "trivial", "normal", [], False, False, "en", "cleanup, not the simplify skill: no diff review"),
    ("N027", "Add a section to the README describing how to run the tests locally", N, [], "trivial", "normal", [], False, False, "en", "repo docs, not a shareable doc"),
    ("N028", "Explain the CAP theorem with a simple example", N, [], "trivial", "normal", [], False, False, "en", "general knowledge"),
    ("N029", "Set up ESLint and Prettier for this TypeScript project", N, [], "normal", "normal", [], False, False, "en", "project tooling, not Claude Code config"),
    ("N030", "The migration script fails with 'relation already exists', help me fix it", N, [], "normal", "normal", [], False, False, "en", "debugging"),
    ("N031", "Profile this function and tell me why it's slow", N, [], "normal", "normal", [], False, False, "en", "performance"),
    ("N032", "Add structured logging to the payment webhook handler", N, [], "trivial", "normal", [], False, False, "en", "feature"),
    ("N033", "Write a SQL migration adding an index on orders.created_at", N, [], "trivial", "normal", [], False, False, "en", "coding"),
    ("N034", "Should I use Redis or Memcached for session storage?", N, [], "trivial", "normal", [], False, False, "en", "advice"),
    ("N035", "Translate this Python function to Go", N, [], "trivial", "normal", [], False, False, "en", "coding"),
    ("N036", "Find where we set the JWT expiry and make it configurable via an env var", N, [], "normal", "normal", [], False, False, "en", "feature"),
    ("N037", "Hi, how's it going?", N, [], "trivial", "normal", [], False, False, "en", "chit-chat"),
    ("N038", "Thanks, that worked!", N, [], "trivial", "normal", [], False, False, "en", "chit-chat"),
    ("N039", "continue", N, [], "trivial", "normal", [], False, False, "en", "continuation"),
    ("N040", "Write a docstring for the calculate_discount function", N, [], "trivial", "normal", [], False, False, "en", "code docs"),
    ("N041", "Почему этот запрос к Postgres такой медленный? EXPLAIN показывает seq scan", N, [], "normal", "normal", [], False, False, "ru", "debugging"),
    ("N042", "Перепиши эту функцию так, чтобы она не мутировала входной список", N, [], "trivial", "normal", [], False, False, "ru", "refactor"),
    ("N043", "Добавь обработку таймаута в клиент API", N, [], "trivial", "normal", [], False, False, "ru", "feature"),
    ("N044", "Объясни, как работает async/await в Python", N, [], "trivial", "normal", [], False, False, "ru", "explanation"),
    ("N045", "Исправь ошибку: TypeError: cannot read properties of undefined (reading 'map')", N, [], "trivial", "normal", [], False, False, "ru", "debugging"),
    ("N046", "Сделай так, чтобы скрипт читал настройки из переменных окружения, а не из хардкода", N, [], "trivial", "normal", [], False, False, "ru", "refactor"),
    ("N047", "Напиши функцию, которая группирует заказы по дню недели", N, [], "trivial", "normal", [], False, False, "ru", "coding"),
    ("N048", "Что лучше для этого проекта — FastAPI или Django?", N, [], "trivial", "normal", [], False, False, "ru", "advice"),
    ("N049", "Добавь тесты для модуля авторизации", N, [], "normal", "normal", [], False, False, "ru", "coding"),
    ("N050", "Посмотри логи деплоя и скажи, почему упал контейнер", N, [], "normal", "normal", [], False, False, "ru", "debugging"),

    # ---------------------------------------------------------------- S: one skill clearly needed (50)
    ("S001", "Review my current branch for bugs before I open the PR", "code-review", [], "trivial", "normal", [], False, False, "en", "diff review for bugs"),
    ("S002", "Do a security review of the changes on this branch", "security-review", [], "trivial", "normal", [], False, False, "en", "branch security review"),
    ("S003", "Clean up the code I just changed: remove duplication and simplify it, then apply the fixes", "simplify", ["code-review"], "trivial", "normal", [], False, False, "en", "simplify's exact trigger"),
    ("S004", "Start the app and take a screenshot of the dashboard page so I can see the new layout", "run", [], "trivial", "normal", [], False, False, "en", "launch + screenshot"),
    ("S005", "Create a CLAUDE.md for this repository", "init", [], "trivial", "normal", [], False, False, "en", "init's exact trigger"),
    ("S006", "What's the context window of Claude Opus 5.5 and how much does it cost per million tokens?", "claude-api", [], "trivial", "normal", [], False, False, "en", "pricing/limits must not come from memory"),
    ("S007", "Add a hook that runs prettier after every file edit", "update-config", [], "trivial", "normal", [], False, False, "en", "hooks live in settings.json"),
    ("S008", "Rebind ctrl+k to clear the input", "keybindings-help", [], "trivial", "normal", [], False, False, "en", "Claude Code keybinding"),
    ("S009", "I keep getting asked to approve git status and ls, can you allowlist the safe read-only commands?", "fewer-permission-prompts", ["update-config"], "trivial", "normal", [], False, False, "en", "exact trigger"),
    ("S010", "Every 5 minutes, check whether the CI run has finished", "loop", [], "trivial", "normal", [], False, False, "en", "in-session interval"),
    ("S011", "Set up a cloud agent that runs our dependency audit every Monday at 9am", "schedule", ["anthropic-skills:schedule"], "trivial", "normal", [], False, False, "en", "scheduled cloud agent"),
    ("S012", "Write a Workflow script that fans out three agents to review different modules", "workflow-authoring", [], "trivial", "normal", [], False, False, "en", "workflow script"),
    ("S013", "Make a bar chart of monthly revenue from this CSV", "dataviz", [], "trivial", "normal", [], False, False, "en", "chart"),
    ("S014", "Build me an artifact page that shows our team's on-call rotation", "artifact-design", ["artifact-capabilities"], "trivial", "normal", [], False, False, "en", "artifact page"),
    ("S015", "Draw an architecture diagram of the sidecar and the hook as an artifact with inline SVG", "artifact-diagramming", ["artifact-design"], "trivial", "normal", [], False, False, "en", "artifact diagram"),
    ("S016", "Make the artifact page remember what each viewer checked off, shared across the whole team", "artifact-capabilities", [], "normal", "normal", [], False, False, "en", "shared state"),
    ("S017", "Write up a PRD for the new export feature as a doc the team can comment on", "anthropic-skills:docs", [], "trivial", "normal", [], False, False, "en", "living doc"),
    ("S018", "Create a Word document with the contract template, with headers and a table of fees", "anthropic-skills:docx", [], "trivial", "normal", [], False, False, "en", "docx deliverable"),
    ("S019", "Make a 10-slide PowerPoint deck from these quarterly results", "anthropic-skills:pptx", [], "trivial", "normal", [], False, False, "en", "pptx deliverable"),
    ("S020", "Clean up this messy spreadsheet: merge the duplicate header rows, fix the date column and save it as xlsx", "anthropic-skills:xlsx", [], "trivial", "normal", [], False, False, "en", "xlsx deliverable"),
    ("S021", "Merge these three PDFs into one and add page numbers", "anthropic-skills:pdf", [], "trivial", "normal", [], False, False, "en", "pdf manipulation"),
    ("S022", "Create a new skill that formats our commit messages according to our convention", "anthropic-skills:skill-creator", [], "trivial", "normal", [], False, False, "en", "skill creation"),
    ("S023", "Go through my memory files and merge the duplicates", "anthropic-skills:consolidate-memory", [], "trivial", "normal", [], False, False, "en", "memory consolidation"),
    ("S024", "I exported my ChatGPT memories, import them into your memory", "anthropic-skills:import-memory", [], "trivial", "normal", [], False, False, "en", "memory import"),
    ("S025", "Create a scheduled task that runs automatically every Friday afternoon and summarizes my week", "anthropic-skills:schedule", ["schedule"], "normal", "normal", [], False, False, "en", "both schedule skills plausible"),
    ("S026", "Where did my tokens go in this session?", "anthropic-skills:explain-usage", [], "trivial", "normal", [], False, False, "en", "usage explanation"),
    ("S027", "Show me my morning brief", "anthropic-skills:morning", [], "trivial", "normal", [], False, False, "en", "morning brief"),
    ("S028", "Help me set up Claude: install plugins that fit a data analyst and connect my tools", "anthropic-skills:setup-claude", [], "trivial", "normal", [], False, False, "en", "guided setup"),
    ("S029", "Implement this Figma design as a React component: https://figma.com/design/abc123/Landing?node-id=1-2", "figma:figma-design-to-code", [], "trivial", "normal", [], False, False, "en", "design to code"),
    ("S030", "In the Figma file, create color variables for our brand palette", "figma:figma-use", ["figma:figma-generate-library"], "normal", "normal", [], False, False, "en", "write action in a Figma file"),
    ("S031", "Make a FigJam flowchart of the checkout process", "figma:figma-generate-diagram", [], "trivial", "normal", [], False, False, "en", "FigJam diagram"),
    ("S032", "Build a design system in Figma from our codebase's tokens and components", "figma:figma-generate-library", [], "trivial", "normal", [], False, False, "en", "design system"),
    ("S033", "Map our Button component to the Figma Button with Code Connect", "figma:figma-code-connect", [], "trivial", "normal", [], False, False, "en", "Code Connect"),
    ("S034", "Create a new blank FigJam file for the retro", "figma:figma-create-new-file", [], "trivial", "normal", [], False, False, "en", "new file"),
    ("S035", "Implement the hover animation from this Figma prototype in our CSS", "figma:figma-implement-motion", ["figma:figma-design-to-code"], "normal", "normal", [], False, False, "en", "motion"),
    ("S036", "Create a procedural noise shader fill in Figma for the hero background", "figma:figma-shaders", [], "trivial", "normal", [], False, False, "en", "shader"),
    ("S037", "Extract all the tables from this PDF invoice into CSV", "anthropic-skills:pdf", ["anthropic-skills:xlsx"], "trivial", "normal", [], False, False, "en", "pdf extraction"),
    ("S038", "Fill in the fields of this PDF form with the data from the JSON", "anthropic-skills:pdf", [], "trivial", "normal", [], False, False, "en", "pdf form"),
    ("S039", "Read the attached .docx and list all the tracked changes", "anthropic-skills:docx", [], "trivial", "normal", [], False, False, "en", "docx read"),
    ("S040", "Add a speaker-notes section to every slide in deck.pptx", "anthropic-skills:pptx", [], "trivial", "normal", [], False, False, "en", "pptx edit"),
    ("S041", "What are the rate limits for the Claude API on tier 2?", "claude-api", [], "trivial", "normal", [], False, False, "en", "rate limits"),
    ("S042", "Allow npm test and npm run lint without asking me every time", "update-config", ["fewer-permission-prompts"], "normal", "normal", [], False, False, "en", "permission allowlist"),
    ("S043", "Run /code-review on PR 128", "code-review", [], "trivial", "normal", [], False, False, "en", "explicit slash"),
    ("S044", "Сделай ревью моей ветки перед мержем", "code-review", [], "trivial", "normal", [], False, False, "ru", "diff review"),
    ("S045", "Собери презентацию на 8 слайдов по итогам квартала в pptx", "anthropic-skills:pptx", [], "trivial", "normal", [], False, False, "ru", "pptx"),
    ("S046", "Сделай график продаж по месяцам из этого CSV", "dataviz", [], "trivial", "normal", [], False, False, "ru", "chart"),
    ("S047", "Объедини эти два PDF в один файл", "anthropic-skills:pdf", [], "trivial", "normal", [], False, False, "ru", "pdf merge"),
    ("S048", "Создай Word-документ с шаблоном договора", "anthropic-skills:docx", [], "trivial", "normal", [], False, False, "ru", "docx"),
    ("S049", "Сколько стоит Claude Sonnet 5 за миллион входных токенов?", "claude-api", [], "trivial", "normal", [], False, False, "ru", "pricing"),
    ("S050", "Настрой хук, чтобы после каждого редактирования запускался линтер", "update-config", [], "trivial", "normal", [], False, False, "ru", "hook"),

    # ---------------------------------------------------------------- K: keyword traps (30)
    ("K001", "Is this regex vulnerable to ReDoS? ^(a+)+$ Please review its security.", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "security-review is for a branch diff, not one pattern"),
    ("K002", "Our PDF export is broken: the generated files won't open in Acrobat. The generator is in export/pdf_writer.py", N, [], "normal", "keyword_trap", ["debug_vs_author"], True, False, "en", "a bug in their generator, not PDF manipulation"),
    ("K003", "Explain how Excel stores dates internally, I keep seeing numbers like 45123", N, [], "normal", "keyword_trap", ["explain_vs_do"], True, False, "en", "knowledge question"),
    ("K004", "Write a paragraph explaining what MCP is for our onboarding guide", N, [], "normal", "keyword_trap", ["explain_vs_do"], True, False, "en", "prose, no tool"),
    ("K005", "Fix this bug in our code that generates PowerPoint slides: the title placeholder always comes out empty", N, ["anthropic-skills:pptx"], "normal", "keyword_trap", ["debug_vs_author"], True, False, "en", "debugging; pptx skill's python-pptx notes could help"),
    ("K006", "Summarize this documentation about GitHub Actions caching for me", N, [], "trivial", "keyword_trap", [], True, False, "en", "summary, no integration"),
    ("K007", "The scheduled job isn't firing anymore since we moved the server to UTC, can you debug the cron expression 0 9 * * 1-5?", N, [], "normal", "keyword_trap", ["debug_vs_author"], True, False, "en", "debugging an existing job"),
    ("K008", "Our Figma plugin's build fails with a TypeScript error in the manifest typing", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "developing plugin code, not acting in a file"),
    ("K009", "Explain what a design token is", N, [], "trivial", "keyword_trap", ["explain_vs_do"], True, False, "en", "definition"),
    ("K010", "python-docx throws KeyError: 'word/document.xml' when I open files generated by LibreOffice, what's going on?", N, ["anthropic-skills:docx"], "normal", "keyword_trap", ["debug_vs_author"], True, False, "en", "library debugging"),
    ("K011", "I read somewhere that Claude has a 1M context window. Anyway, can you fix the off-by-one in this loop?", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "claude keyword is context"),
    ("K012", "Rename the security_review_enabled flag to audit_enabled across the codebase", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "identifier contains skill name"),
    ("K013", "Our xlsx upload endpoint rejects files larger than 10MB, raise the limit to 50MB", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "server config"),
    ("K014", "Write a Python function that validates a cron expression", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "coding"),
    ("K015", "Add a loop that retries the request three times", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "loop as code construct"),
    ("K016", "In the README, add a badge that links to our PDF user manual", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "markdown edit"),
    ("K017", "Why does my Chart.js line chart not render when the data array is empty?", N, [], "normal", "keyword_trap", ["debug_vs_author"], True, False, "en", "library bug"),
    ("K018", "The morning cron job that emails the sales report fails with an SMTP auth error", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "morning != morning brief"),
    ("K019", "Our memory usage grows until the container OOMs, find the leak", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "memory != Claude memory"),
    ("K020", "Import the utils module lazily to cut startup time", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "import != import-memory"),
    ("K021", "The Run button in our web app does nothing when clicked, fix the click handler", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "run != run skill"),
    ("K022", "Initialize the database connection pool at startup instead of per request", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "init != init skill"),
    ("K023", "Update the config loader so it supports YAML as well as JSON", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "app config, not Claude Code config"),
    ("K024", "Add a keyboard shortcut to our Electron app: Ctrl+S saves the draft", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "app shortcut, not Claude Code keybindings"),
    ("K025", "Simplify this fraction-reduction algorithm, it's O(n) and should use gcd", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "algorithm change, not diff cleanup"),
    ("K026", "The code review bot on our GitHub posts duplicate comments, fix its dedup logic", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "coding a bot"),
    ("K027", "Our WebGL shader compiles on desktop but renders black on iOS Safari", N, [], "normal", "keyword_trap", ["context_only_keyword"], True, False, "en", "shader != Figma shader"),
    ("K028", "Write docstrings for the public API of our PDF parsing module", N, [], "trivial", "keyword_trap", ["context_only_keyword"], True, False, "en", "code docs"),
    ("K029", "Почему наш экспорт в Excel ломает кодировку кириллицы?", N, [], "normal", "keyword_trap", ["debug_vs_author"], True, False, "ru", "exporter bug"),
    ("K030", "Объясни, чем PDF/A отличается от обычного PDF", N, [], "trivial", "keyword_trap", ["explain_vs_do"], True, False, "ru", "knowledge question"),

    # ---------------------------------------------------------------- E: skill's topic, skill not needed (20)
    ("E001", "How does Claude Code decide when to run a hook? Just explain, don't change anything.", N, ["update-config"], "normal", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "explanation; the skill's reference could help"),
    ("E002", "What's the difference between a .docx and an .odt file?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge"),
    ("E003", "In general, when should a team use a slide deck versus a written memo?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "advice"),
    ("E004", "What is a pivot table, conceptually?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge"),
    ("E005", "How do PDF forms work under the hood, AcroForm versus XFA?", N, ["anthropic-skills:pdf"], "normal", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge"),
    ("E006", "Tell me how I would set up a cron-scheduled cloud agent. I'll do it myself later.", N, ["schedule"], "normal", "explanation_vs_action", ["tell_how_vs_do"], False, False, "en", "how-to; the skill knows the mechanism"),
    ("E007", "What makes a good data visualization? I'm preparing a talk about it.", N, ["dataviz"], "normal", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "talk prep, not a chart"),
    ("E008", "Explain what Figma Code Connect is for", N, ["figma:figma-code-connect"], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "definition"),
    ("E009", "What's the general idea behind code review checklists?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "advice"),
    ("E010", "Why do people say CLAUDE.md files should be kept short?", N, ["init"], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "advice"),
    ("E011", "Explain the structure of an .xlsx file, is it just zipped XML?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge"),
    ("E012", "What are skills in Claude Code and how are they different from slash commands?", N, ["anthropic-skills:skill-creator"], "normal", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "concept question"),
    ("E013", "Is it better to put charts in the slides or in an appendix?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "advice"),
    ("E014", "How do FigJam boards differ from Figma design files?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge"),
    ("E015", "What's the security risk of storing JWTs in localStorage?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "knowledge; no diff"),
    ("E016", "Explain what 'user-invocable-only' means for a skill override", N, ["update-config"], "normal", "explanation_vs_action", ["explain_vs_do"], False, False, "en", "concept"),
    ("E017", "Объясни, как устроен формат PPTX внутри", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "ru", "knowledge"),
    ("E018", "Расскажи в общих чертах, что такое Figma variables и зачем они нужны", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "ru", "knowledge"),
    ("E019", "Как вообще работают cron-выражения? Просто объясни.", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "ru", "knowledge"),
    ("E020", "Что такое code review и зачем оно команде?", N, [], "trivial", "explanation_vs_action", ["explain_vs_do"], False, False, "ru", "knowledge"),

    # ---------------------------------------------------------------- I: skill needed, its keywords absent (20)
    ("I001", "Before I merge, go over what I changed and flag anything that could break in production", "code-review", ["security-review"], "normal", "adversarial", ["implicit_need"], True, False, "en", "diff review without the words"),
    ("I002", "Could someone with bad intentions exploit anything in the diff I'm about to push?", "security-review", ["code-review"], "normal", "adversarial", ["implicit_need"], True, False, "en", "security review without the word"),
    ("I003", "I need something I can click through while presenting the Q3 numbers to the board on Thursday", "anthropic-skills:pptx", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "a deck, never named"),
    ("I004", "Show me visually how revenue moved month to month from this data", "dataviz", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "a chart, never named"),
    ("I005", "Keep an eye on the deploy and tell me here whenever its status changes, checking every couple of minutes", "loop", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "interval polling"),
    ("I006", "Every night at 2am, have an agent go through the open issues and label them", "schedule", ["anthropic-skills:schedule"], "normal", "adversarial", ["implicit_need"], True, False, "en", "recurring agent"),
    ("I007", "Stop asking me for permission every time you read a file", "fewer-permission-prompts", ["update-config"], "normal", "adversarial", ["implicit_need"], True, False, "en", "permission prompts"),
    ("I008", "Our designer shared a mockup link (design/XyZ/Landing?node-id=4-12), turn it into working code", "figma:figma-design-to-code", [], "complex", "adversarial", ["implicit_need"], True, False, "en", "Figma never named"),
    ("I009", "The file from accounting has three header rows and dates in five formats; clean it up so I can load it into pandas, and give me back the fixed file", "anthropic-skills:xlsx", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "messy spreadsheet"),
    ("I010", "Take these three scanned contracts and make the text searchable", "anthropic-skills:pdf", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "OCR"),
    ("I011", "How much is this session costing me so far, and what's eating the budget?", "anthropic-skills:explain-usage", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "usage"),
    ("I012", "Teach yourself a reusable procedure for how we write release notes, so you do it the same way every time", "anthropic-skills:skill-creator", [], "complex", "adversarial", ["implicit_need"], True, False, "en", "a skill, never named"),
    ("I013", "Your notes about me are getting cluttered and contradict each other, tidy them up", "anthropic-skills:consolidate-memory", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "memory"),
    ("I014", "Make ctrl+enter send the message instead of adding a newline", "keybindings-help", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "keybinding"),
    ("I015", "Whenever you finish editing a TypeScript file, run tsc automatically", "update-config", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "a hook, never named"),
    ("I016", "Give me a page my team can open that tracks who's bringing what to the offsite, where everyone sees each other's entries", "artifact-capabilities", ["artifact-design"], "complex", "adversarial", ["implicit_need"], True, False, "en", "shared-state artifact"),
    ("I017", "What's the newest and most capable model I can call from the SDK, and what is its exact id?", "claude-api", [], "normal", "adversarial", ["implicit_need"], True, False, "en", "model ids"),
    ("I018", "Перед тем как отправлю PR, пройдись по моим изменениям и найди, что может сломаться", "code-review", [], "normal", "adversarial", ["implicit_need"], True, False, "ru", "diff review"),
    ("I019", "Нужно что-то, что я смогу листать на созвоне с инвесторами: 7 экранов с ключевыми цифрами", "anthropic-skills:pptx", [], "normal", "adversarial", ["implicit_need"], True, False, "ru", "a deck"),
    ("I020", "Сколько я уже потратил токенов в этой сессии и на что?", "anthropic-skills:explain-usage", [], "normal", "adversarial", ["implicit_need"], True, False, "ru", "usage"),

    # ---------------------------------------------------------------- D: similar skills compete (15)
    ("D001", "Remind me every weekday at 8am to review the overnight alerts", "anthropic-skills:schedule", ["schedule"], "normal", "similar_skills", [], False, False, "en", "two schedule skills"),
    ("D002", "Run /babysit-prs every 10 minutes while I'm in this session", "loop", [], "normal", "similar_skills", [], False, False, "en", "in-session loop, not cron"),
    ("D003", "Set up a routine in the cloud that runs every hour even when my laptop is off", "schedule", ["anthropic-skills:schedule"], "normal", "similar_skills", [], False, False, "en", "cloud routine, not loop"),
    ("D004", "Just tidy up the code I changed, remove duplication, don't hunt for bugs", "simplify", [], "normal", "similar_skills", [], False, False, "en", "simplify, not code-review"),
    ("D005", "Find correctness bugs in this PR, I don't care about style", "code-review", [], "normal", "similar_skills", [], False, False, "en", "code-review, not simplify"),
    ("D006", "Check this branch's changes for injection and auth bypass issues", "security-review", ["code-review"], "normal", "similar_skills", [], False, False, "en", "security-review, not code-review"),
    ("D007", "Write a spec the team can comment on and keep editing together", "anthropic-skills:docs", [], "normal", "similar_skills", [], False, False, "en", "docs, not docx"),
    ("D008", "I need a .docx file of the spec to email to legal", "anthropic-skills:docx", [], "normal", "similar_skills", [], False, False, "en", "docx, not docs"),
    ("D009", "Draw the service architecture as an inline SVG diagram in the artifact", "artifact-diagramming", ["artifact-design"], "normal", "similar_skills", [], False, False, "en", "artifact diagram, not FigJam"),
    ("D010", "Draw the service architecture as a diagram in FigJam", "figma:figma-generate-diagram", [], "normal", "similar_skills", [], False, False, "en", "FigJam, not artifact"),
    ("D011", "Create a brand new empty Figma design file called 'Onboarding v2'", "figma:figma-create-new-file", ["figma:figma-use"], "normal", "similar_skills", [], False, False, "en", "new file, not figma-use"),
    ("D012", "In the existing Figma file, add a 'disabled' variant to the Button component", "figma:figma-use", ["figma:figma-generate-library"], "normal", "similar_skills", [], False, False, "en", "edit, not new file"),
    ("D013", "Bring in the memory export from Gemini that I pasted above", "anthropic-skills:import-memory", [], "normal", "similar_skills", [], False, False, "en", "import, not consolidate"),
    ("D014", "Add a line chart to the artifact dashboard showing daily signups", "dataviz", ["artifact-design"], "normal", "similar_skills", [], False, False, "en", "chart inside an artifact"),
    ("D015", "Add a PreToolUse hook that blocks rm -rf", "update-config", [], "normal", "similar_skills", [], False, False, "en", "hook, not permission allowlist"),

    # ---------------------------------------------------------------- C: compound / multi-part (15)
    ("C001", "Review my branch for bugs and then make a short slide deck summarizing what changed for the team", "code-review", ["anthropic-skills:pptx"], "complex", "compound", ["multi_domain"], False, False, "en", "two skills; review first"),
    ("C002", "Pull the tables out of this PDF report and build a chart of the yearly totals", "anthropic-skills:pdf", ["dataviz"], "complex", "compound", ["multi_domain"], False, False, "en", "extract then chart"),
    ("C003", "Fix the failing test in auth.py, then commit and push", N, [], "normal", "compound", [], False, False, "en", "all ordinary work"),
    ("C004", "Refactor the parser, add tests, and update the README", N, [], "complex", "compound", [], False, False, "en", "all ordinary work"),
    ("C005", "Set up a hook that runs the tests after edits, and also rebind ctrl+t to toggle the todo list", "update-config", ["keybindings-help"], "complex", "compound", ["multi_domain"], False, False, "en", "config first"),
    ("C006", "Clean up this CSV of survey results and produce an Excel file with a summary sheet and a chart", "anthropic-skills:xlsx", ["dataviz"], "complex", "compound", ["multi_domain"], False, False, "en", "xlsx deliverable"),
    ("C007", "Implement this Figma screen in React and make the button hover animation match the prototype", "figma:figma-design-to-code", ["figma:figma-implement-motion"], "complex", "compound", ["multi_domain"], False, False, "en", "design then motion"),
    ("C008", "Read the attached contract PDF, then write a Word memo summarizing the risks", "anthropic-skills:docx", ["anthropic-skills:pdf"], "complex", "compound", ["multi_domain"], False, False, "en", "deliverable is docx"),
    ("C009", "Explain how our caching layer works and then draw it as a diagram in FigJam", "figma:figma-generate-diagram", [], "complex", "compound", ["multi_domain"], False, False, "en", "explanation + FigJam"),
    ("C010", "Profile the slow endpoint, fix it, and write up the findings as a doc the team can comment on", "anthropic-skills:docs", [N], "complex", "compound", ["multi_domain"], False, False, "en", "mostly coding; doc at the end"),
    ("C011", "Every morning at 9, pull yesterday's error counts and email me a chart", "anthropic-skills:schedule", ["schedule", "dataviz"], "complex", "compound", ["multi_domain"], False, False, "en", "recurring + chart"),
    ("C012", "Make a PR-ready change: rename the config key, update the docs, and bump the version", N, [], "normal", "compound", [], False, False, "en", "all ordinary work"),
    ("C013", "Сделай ревью ветки и заодно проверь её на уязвимости", "code-review", ["security-review"], "complex", "compound", ["multi_domain"], False, False, "ru", "review + security"),
    ("C014", "Вытащи данные из этого PDF и сделай по ним таблицу Excel", "anthropic-skills:pdf", ["anthropic-skills:xlsx"], "complex", "compound", ["multi_domain"], False, False, "ru", "pdf then xlsx"),
    ("C015", "Почини баг в экспорте и потом подготовь пару слайдов для команды о том, что сломалось", "anthropic-skills:pptx", [N], "complex", "compound", ["multi_domain"], False, False, "ru", "mostly coding; slides at the end"),

    # ---------------------------------------------------------------- A: mandated adversarial + special forms (23)
    ("A001", "Review the security of this regex: ^([a-zA-Z0-9]+\\s?)*$", N, [], "normal", "adversarial", ["context_only_keyword"], True, False, "en", "mandated: security != security-review"),
    ("A002", "Why is our PDF export broken?", N, [], "normal", "adversarial", ["debug_vs_author"], True, False, "en", "mandated: PDF != PDF skill"),
    ("A003", "Explain how Excel stores dates", N, [], "normal", "adversarial", ["explain_vs_do"], True, False, "en", "mandated"),
    ("A004", "Write a paragraph explaining MCP", N, [], "normal", "adversarial", ["explain_vs_do"], True, False, "en", "mandated"),
    ("A005", "Fix this bug in code that generates PowerPoint slides", N, ["anthropic-skills:pptx"], "normal", "adversarial", ["debug_vs_author"], True, False, "en", "mandated: debugging, pptx acceptable"),
    ("A006", "Summarize this documentation about GitHub Actions", N, [], "normal", "adversarial", [], True, False, "en", "mandated"),
    ("A007", "Don't make a slide deck, just give me three bullet points I can paste into Slack", N, [], "normal", "adversarial", ["negation"], True, False, "en", "negated deliverable"),
    ("A008", "No need for a chart, just tell me which month had the highest revenue", N, [], "normal", "adversarial", ["negation"], True, False, "en", "negated deliverable"),
    ("A009", "I don't want a PDF, send the report as plain markdown in the chat", N, [], "normal", "adversarial", ["negation"], True, False, "en", "negated deliverable"),
    ("A010", "Не надо никаких презентаций, просто перечисли основные риски", N, [], "normal", "adversarial", ["negation"], True, False, "ru", "negated deliverable"),
    ("A011", "Oh great, another 40-page PDF from legal. Would be lovely if someone pulled out just the termination clauses for me.", "anthropic-skills:pdf", [N], "complex", "adversarial", ["sarcasm"], True, False, "en", "indirect request to extract from a PDF"),
    ("A012", "Sure, I'll just keep clicking 'allow' on every single ls forever, that's totally fine.", "fewer-permission-prompts", ["update-config", N], "complex", "adversarial", ["sarcasm"], True, False, "en", "sarcastic ask to reduce prompts"),
    ("A013", "Love how the reviewer found nothing last time. Anyway, the auth refactor is done, have a look before it ships?", "code-review", ["security-review"], "complex", "adversarial", ["sarcasm"], True, False, "en", "indirect review request"),
    ("A014", "Can you do the thing with the slides again?", "anthropic-skills:pptx", [N], "normal", "adversarial", ["ambiguous_wording"], True, True, "en", "no context: which thing?"),
    ("A015", "Make it look nicer", N, ["artifact-design"], "normal", "adversarial", ["ambiguous_wording"], True, True, "en", "no context at all"),
    ("A016", "Tell me how to merge PDFs on the command line, I want to learn it, not have you do it", N, ["anthropic-skills:pdf"], "normal", "adversarial", ["tell_how_vs_do"], True, False, "en", "tell how"),
    ("A017", "Merge these PDFs for me: a.pdf and b.pdf into combined.pdf", "anthropic-skills:pdf", [], "trivial", "adversarial", ["tell_how_vs_do"], True, False, "en", "do it"),
    ("A018", "How would I write a hook that blocks force pushes? Just show me the JSON, I'll add it myself.", "update-config", [N], "normal", "adversarial", ["tell_how_vs_do"], True, False, "en", "the hook schema reference is the point"),
    ("A019", "Our Figma export script crashes while writing the PDF, and the Excel summary it produces has wrong totals, debug the script", N, [], "complex", "adversarial", ["multi_domain", "context_only_keyword"], True, False, "en", "three domain keywords, one debugging task"),
    ("A020", "Compare our PDF, Excel and PowerPoint export code paths and tell me which one has the memory leak", N, [], "complex", "adversarial", ["multi_domain", "context_only_keyword"], True, False, "en", "keywords as context"),
    ("A021", "While I was making the board deck I noticed CI is red. Can you look at why test_orders fails?", N, [], "normal", "adversarial", ["context_only_keyword"], True, False, "en", "deck is context"),
    ("A022", "The designer is in Figma all day; meanwhile I need the API to return 404 instead of 500 for missing users", N, [], "normal", "adversarial", ["context_only_keyword"], True, False, "en", "Figma is context"),
    ("A023", "Я делал презентацию и заметил, что в API опечатка в поле 'adress', поправь везде", N, [], "normal", "adversarial", ["context_only_keyword"], True, False, "ru", "presentation is context"),
]

# Real-world prompts: the three actual user prompts from Claude Code sessions in this project
# (the only scope where sending prompt text to TypeSafe was agreed). The texts live in the
# git-ignored private file; only their labels are here.
REAL = [
    ("R001", "cc829fbe", "dev", N, ["update-config"], "complex", "long multi-stage engineering spec (Jev setup); hook install is one step"),
    ("R002", "0f9f41ce", "test", N, ["update-config"], "complex", "long multi-stage spec (Mercury + sidecar + shadow hook)"),
    ("R003", "82768e2b", "test", N, [], "complex", "long benchmark spec that names skills, hooks and PDF/Excel/PowerPoint as topics"),
]

GROUP_OF = {"N": "no_skill", "S": "one_skill", "K": "keyword_trap", "E": "topic_not_needed",
            "I": "implicit_need", "D": "similar_skills", "C": "compound", "A": "adversarial_mandated",
            "R": "real_world"}


def label_row(cid, expected, acceptable, difficulty, category, tags, adversarial, ambiguous,
              lang, rationale, source):
    return {"id": cid, "expected": expected, "acceptable": acceptable,
            "skill_required": expected is not None, "difficulty": difficulty,
            "category": category, "group": GROUP_OF[cid[0]], "tags": tags,
            "adversarial": adversarial, "ambiguous": ambiguous, "lang": lang,
            "source": source, "rationale": rationale}


def real_prompts() -> dict[str, str]:
    """Read the three real prompts from the local Claude Code transcripts, sanitised."""
    sys.path.insert(0, str(HERE.parent / "hooks"))
    import prompt_collector as pc
    tdir = Path.home() / ".claude" / "projects" / "C--sarychev-Codex-jev-test"
    out = {}
    for cid, sess, *_ in REAL:
        files = list(tdir.glob(f"{sess}*.jsonl"))
        if not files:
            continue
        for line in files[0].read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") != "user" or o.get("isMeta"):
                continue
            c = o.get("message", {}).get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if b.get("type") == "text")
            if isinstance(c, str) and len(c) > 1000:
                if pc.looks_secret(c):
                    raise SystemExit(f"{cid}: secret-shaped content, refusing to store it")
                # Redact but do not truncate: the router in production sees the whole prompt.
                for pat, repl in pc.REDACTIONS:
                    c = pat.sub(repl, c)
                out[cid] = c
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate ids"
    target = HERE / "router_test_prompts.jsonl"
    if target.exists() and not args.force:
        raise SystemExit("dataset already built; --force only before any test run exists")
    if (HERE.parent / "frozen" / "FINAL_FREEZE.json").exists():
        raise SystemExit("final freeze exists — the dataset can no longer be rebuilt")

    rng = random.Random(SEED)
    by_group: dict[str, list] = {}
    for c in CASES:
        by_group.setdefault(c[0][0], []).append(c)
    split = {}
    for g, cs in sorted(by_group.items()):
        cs = sorted(cs, key=lambda c: c[0])
        rng.shuffle(cs)
        k = round(len(cs) * DEV_FRACTION)
        for i, c in enumerate(cs):
            split[c[0]] = "dev" if i < k else "test"

    rows = {"dev": ([], []), "test": ([], [])}
    for (cid, prompt, exp, acc, diff, cat, tags, adv, amb, lang, why) in CASES:
        p, l = rows[split[cid]]
        p.append({"id": cid, "prompt": prompt})
        l.append(label_row(cid, exp, acc, diff, cat, tags, adv, amb, lang, why, "synthetic"))

    texts = real_prompts()
    PRIVATE.mkdir(exist_ok=True)
    with (PRIVATE / "router_real_prompts.jsonl").open("w", encoding="utf-8") as fh:
        for cid, _sess, sp, exp, acc, diff, why in REAL:
            if cid not in texts:
                print(f"  {cid}: transcript not found, skipped")
                continue
            fh.write(json.dumps({"id": cid, "split": sp, "prompt": texts[cid]}, ensure_ascii=False) + "\n")
            rows[sp][1].append(label_row(cid, exp, acc, diff, "real_world", ["long_spec"], False,
                                         False, "ru", why, "real_world"))

    for sp, (p, l) in rows.items():
        with (HERE / f"router_{sp}_prompts.jsonl").open("w", encoding="utf-8") as fh:
            for r in sorted(p, key=lambda r: r["id"]):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        with (HERE / f"router_{sp}_labels.jsonl").open("w", encoding="utf-8") as fh:
            for r in sorted(l, key=lambda r: r["id"]):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{sp}: {len(l)} cases ({len(p)} synthetic + {len(l) - len(p)} real)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
