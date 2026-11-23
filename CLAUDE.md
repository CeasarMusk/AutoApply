# AutoApply

Python CLI (typer + rich + questionary) that keeps one master resume, finds jobs on public ATS
boards, tailors a one-page resume per job, and fills Greenhouse / Lever / Ashby application forms
in a Playwright browser. The user reviews and clicks Submit; the program never submits.

## Commands
- Run: `uv run autoapply` (interactive console) or `uv run autoapply <cmd>`
- Tests: `uv run pytest -q` (fast, ~4s; browser tests use local HTML and skip if Chromium is missing)
- Browser: `uv run playwright install chromium`
- Try a form without submitting: `uv run autoapply apply <job_id> --dry-run --no-ai --no-tailor`
  (screenshot at `~/.autoapply/out/dry-run-<id>.png`; it's ~1280x6500, so crop it or check the summary table instead of reading it whole)

## Layout (src/autoapply/)
- `cli.py`: root app, `init`, `resume ...` (import/build/edit/show/validate/transcript/github), `usage`
- `cli_ai.py`: `ai` (provider status + picker, also used by `init`), `ai use <provider> [--model]`
- `cli_queue.py`: `queue build/show/add/remove/clear/order/run` (apply queue; `run` calls `cli_apply.apply_one`)
- `cli_jobs.py`: `search`, `jobs`, `job show/open/save/ignore/reset`, `companies`, `profile`
- `cli_apply.py`: `apply` (wraps `apply_one`, which returns an outcome instead of exiting), `tailor`, `answers ...`; tailoring choice menu (use/preview/redo/original)
- `shell.py`: interactive console: OPTIONS (`set X`), JOB_SHORTCUTS (`use <id>` then `apply`), HELP, banner
- `config.py`: `~/.autoapply` paths (`AUTOAPPLY_HOME` overrides), `Config` model, `keep_document`
- `models.py`: `MasterResume` (pydantic, stable ids) + sqlite tables `Job`, `Application`, `QueueItem`, `LLMUsage`
- `llm/client.py`: `LLM.parse(task, system, content, output_format)` → validated pydantic object; backends `claude-code` (subscription via `claude -p`, default), `api`, and `ollama` (local; JSON-schema `format`, PDFs → text, one retry on invalid JSON)
- `sources/`: fetchers per ATS (greenhouse, lever, ashby, smartrecruiters, workable, bamboohr, workday, oracle) + remoteok feed, each with `fetch(http, slug, query=())` and `detail()`; `base.py` = polite requests (per-host spacing, 429 back-off), `Listing.complete`; `urls.py` = URL → (ats, slug, job id); `keryx.py` = offline internship/new-grad list (bundled in `sources/data/`, `keryx sync`, live board checks); `starter.py` = built-in company list
- `jobs/details.py`: fetches a missing description on demand (Keryx/Workday listings have none)
- `jobs/`: `queue.py` apply queue (`QueueItem` table; sources last/saved/all, orders match/recent; `search` saves its matches to `last_search.json`), `search.py` filters, `rank.py` local 0-100 match score (no AI), `profile.py` one cached AI call → search profile, `presets.py` search menu
- `resume/`: `importer.py` (file → MasterResume via Claude), `store.py` (YAML + id assignment), `builder.py` (menu editor), `github.py` (profile → projects: basic = README summary/features, deep = languages + manifests + AI bullets checked with `tailor.check_rewrite` against the repo text), `validate.py`, `review.py`, `tailor.py` (AI picks by id + rewrite guard), `render.py` (Jinja HTML → PDF via Chromium, fits one page)
- `apply/`: `scan.py` (JS form scanner → `Field`s; Greenhouse API enrichment), `fill.py` (fills one field per kind), `answers.py` (standard question keys, `AnswerStore`, `match_option`, `to_iso_date`), `runner.py` (`Applier`: plan → draft essays → ask unknowns → fill → summary → learn edits), `essay.py`

## Rules
- Never click Submit or press Enter in form inputs (Enter can submit). The user submits.
- Tailoring/essays must never invent facts: tailoring selects master items by id; `tailor.check_rewrite` rejects rewrites adding numbers or terms not in the master resume.
- Sensitive answers (work auth, sponsorship, EEO, salary: `answers.SENSITIVE`) are never guessed from the resume; only saved answers or the user.
- Personal data lives in `~/.autoapply`, never in the repo.
- Real-site checks: dry runs only. Ask before running real AI calls (tailor/essays) when a unit test would do.
- Don't hammer job boards: go through `base.request` (spacing + back-off), keep live Keryx checks capped, and only
  mark jobs closed from a `Listing` with `complete=True` (Workday/Oracle fetches are searches, never complete).
- Refresh the bundled Keryx snapshot with `uv run python scripts/update_keryx_snapshot.py [keryx checkout]`.

## Gotchas learned on real forms
- Ashby: inputs may have no id/name; multi-select checkboxes each get their own `name` (grouped by `-labeled-checkbox-N` id); required shown only via CSS; resume parsing can blank fields late (runner refills cleared fields).
- Lever: parses the uploaded resume and overwrites fields → uploads happen first, then `fill.wait_until_idle`.
- Location fields need real keystrokes + picking a suggestion (`fill._type_place`).
- "Authorized to work *without* sponsorship" is a separate key from "require sponsorship" (opposite meaning).
