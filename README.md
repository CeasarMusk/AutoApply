# AutoApply

CLI that keeps one master resume, finds jobs, and helps you apply with AI-tailored
resumes and cover letters. You stay in the loop: the browser fills the form, you click Submit.

## Status
- [x] Master resume: import from PDF/DOCX/TXT, or build/edit step by step
- [x] Interactive console (`autoapply` with no arguments)
- [x] Job search: Greenhouse, Lever, Ashby public job boards
- [ ] Aggregator sources
- [ ] Fit scoring
- [ ] Tailoring + PDF rendering
- [ ] Form filling (Playwright)

## Setup
```bash
uv sync
uv run autoapply init
```

**AI backend** (chosen during `init`, stored as `llm_backend` in `~/.autoapply/config.yaml`):
- `claude-code` (default): uses your Claude Pro/Max subscription through Claude Code's
  headless mode (`claude -p`). Install Claude Code and run `claude` once to log in. Calls count
  toward your normal subscription usage limits. Personal use only.
- `api`: uses the Anthropic API (`export ANTHROPIC_API_KEY=...`), billed per token. Use this if you
  ever share the tool with other people.

## Interactive console

Run `uv run autoapply` with no arguments:

```
autoapply > companies add stripe ramp palantir
autoapply > set KEYWORDS software engineer, backend engineer
autoapply > set EXCLUDE senior, staff, principal, manager
autoapply > set LOCATION Remote, New York
autoapply > search
autoapply > jobs
autoapply > use 12
autoapply job(12:Stripe) > info
autoapply job(12:Stripe) > save
autoapply job(12:Stripe) > back
```

Tab completes commands and options; ↑ / Ctrl-R search history (`~/.autoapply/history`).
Every command below also works directly from the shell: `uv run autoapply search`.

| Console command | What it does |
|---|---|
| `help`, `help <command>` | All commands / one command's options |
| `set <OPTION> <value>`, `unset <OPTION>`, `show options` | Saved settings: KEYWORDS, EXCLUDE, LOCATION, REMOTE, MAX_AGE, BACKEND, MODEL |
| `show jobs\|companies\|resume\|usage` | Shortcuts |
| `use <id>` / `back` | Select a job; `info`, `open`, `save`, `ignore`, `reset` then act on it |
| `banner`, `history`, `clear`, `exit` | |

## Job commands
| Command | What it does |
|---|---|
| `companies add <slug…>` | Add companies; detects whether each uses Greenhouse, Lever or Ashby. The slug is in the company's job-board URL |
| `companies list` / `companies remove <slug>` | Manage the list |
| `search [keywords…] [-l LOC] [--remote] [-x WORD] [--max-age N]` | Fetch all boards, save jobs matching your filters (flags override saved options) |
| `jobs [-s STATUS] [-c COMPANY] [-g TEXT] [--remote] [--all] [-n N]` | List saved jobs |
| `job show <id> [--full]` | Details and description |
| `job open <id>` | Open the posting in your browser |
| `job save\|ignore\|reset <id…>` | Shortlist / hide / clear |

## Resume commands
| Command | What it does |
|---|---|
| `autoapply resume import resume.pdf` | Claude reads the file into `~/.autoapply/master_resume.yaml`; review, fix, save |
| `autoapply resume build` | Step-by-step wizard (with optional AI bullet suggestions) |
| `autoapply resume edit [section]` | Menu editor: add/edit/reorder/delete items and bullets |
| `autoapply resume show` | Print it with problems highlighted |
| `autoapply resume validate` | Check after hand-editing the YAML |
| `autoapply usage` | Token usage per task |

Every item gets a stable id (`exp2`, `exp2.b3`, …). Tailoring will select items by id, so
it can't invent experience you don't have. Put *more* in the master than fits on a page.

Data lives in `~/.autoapply` (override with `AUTOAPPLY_HOME`).

## Tests
```bash
uv run pytest
```
