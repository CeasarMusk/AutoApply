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
- `cli.py`: root app, `init`, `resume ...` (import/build/edit/show/validate/transcript), `usage`
- `cli_jobs.py`: `search`, `jobs`, `job show/open/save/ignore/reset`, `companies`, `profile`
- `cli_apply.py`: `apply`, `tailor`, `answers ...`; tailoring choice menu (use/preview/redo/original)
- `shell.py`: interactive console: OPTIONS (`set X`), JOB_SHORTCUTS (`use <id>` then `apply`), HELP, banner
- `config.py`: `~/.autoapply` paths (`AUTOAPPLY_HOME` overrides), `Config` model, `keep_document`
- `models.py`: `MasterResume` (pydantic, stable ids) + sqlite tables `Job`, `Application`, `LLMUsage`
- `llm/client.py`: `LLM.parse(task, system, content, output_format)` → validated pydantic object; backends `claude-code` (subscription via `claude -p`, default) and `api`
- `sources/`: greenhouse, lever, ashby, remoteok fetchers; `starter.py` = built-in company list
- `jobs/`: `search.py` filters, `rank.py` local 0-100 match score (no AI), `profile.py` one cached AI call → search profile, `presets.py` search menu
- `resume/`: `importer.py` (file → MasterResume via Claude), `store.py` (YAML + id assignment), `builder.py` (menu editor), `validate.py`, `review.py`, `tailor.py` (AI picks by id + rewrite guard), `render.py` (Jinja HTML → PDF via Chromium, fits one page)
- `apply/`: `scan.py` (JS form scanner → `Field`s; Greenhouse API enrichment), `fill.py` (fills one field per kind), `answers.py` (standard question keys, `AnswerStore`, `match_option`, `to_iso_date`), `runner.py` (`Applier`: plan → draft essays → ask unknowns → fill → summary → learn edits), `essay.py`

## Rules
- Never click Submit or press Enter in form inputs (Enter can submit). The user submits.
- Tailoring/essays must never invent facts: tailoring selects master items by id; `tailor.check_rewrite` rejects rewrites adding numbers or terms not in the master resume.
- Sensitive answers (work auth, sponsorship, EEO, salary: `answers.SENSITIVE`) are never guessed from the resume; only saved answers or the user.
- Personal data lives in `~/.autoapply`, never in the repo.
- Real-site checks: dry runs only. Ask before running real AI calls (tailor/essays) when a unit test would do.

## Gotchas learned on real forms
- Ashby: inputs may have no id/name; multi-select checkboxes each get their own `name` (grouped by `-labeled-checkbox-N` id); required shown only via CSS; resume parsing can blank fields late (runner refills cleared fields).
- Lever: parses the uploaded resume and overwrites fields → uploads happen first, then `fill.wait_until_idle`.
- Location fields need real keystrokes + picking a suggestion (`fill._type_place`).
- "Authorized to work *without* sponsorship" is a separate key from "require sponsorship" (opposite meaning).
