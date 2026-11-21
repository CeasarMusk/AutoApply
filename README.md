# AutoApply

CLI that keeps one master resume, finds jobs, and helps you apply with AI-tailored
resumes and cover letters. You stay in the loop: the browser fills the form, you click Submit.

## Status
- [x] Master resume: import from PDF/DOCX/TXT, or build/edit step by step
- [x] Interactive console (`autoapply` with no arguments)
- [x] Job search: Greenhouse, Lever, Ashby public job boards
- [x] Apply: fills Greenhouse / Lever / Ashby forms; you review and click Submit
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

## Quick start

```bash
uv sync
uv run autoapply resume import ~/resume.pdf   # once: your PDF becomes the master resume
uv run autoapply                              # opens the console
autoapply > search
```

`search` opens a menu:
1. **What:** ★ Jobs like my resume, a field (Computer Science & Software ▸ Backend, Frontend,
   Mobile, Security…, Data & AI ▸ …, IT & Support ▸ …, Product & Design ▸ …), or your own keywords.
2. **Level:** Internship, New grad / Entry, Mid, Senior, Any.
3. **Where:** Remote only, United States, Anywhere, or a city.

It searches ~60 built-in tech companies (Greenhouse / Lever / Ashby boards) plus the Remote OK feed
(about 16,000 open jobs, ~15 s), and ranks results by how well they match your resume (titles, skills
and level). "Jobs like my resume" makes one AI call to build a search profile, cached until the resume changes.

| Command | What it does |
|---|---|
| `search` | Menu search (above) |
| `search --last` | Repeat the last search |
| `search "data engineer" --level entry -l US` | Search without the menu |
| `jobs [-s saved] [-g text] [--remote] [--all]` | Saved jobs, best match first |
| `use <id>` → `info` / `open` / `save` / `ignore`, `back` | Work with one job |
| `profile [--refresh]` | What your resume says to search for |
| `companies add <slug>` | Add a company beyond the built-in list |
| `set STARTER false` / `set REMOTEOK false` | Turn off the built-in list / Remote OK |
| `help`, `exit` | |

Jobs that disappear from a company's board are marked `closed` and hidden. The same role
posted in several cities is shown once ("+2 more locations").

## Applying

```
autoapply > use 91
autoapply job(91:Scale AI) > apply
```

A browser window opens on the application and fills it in:
- **Your resume**: name, email, phone, location, links, current company, school, and the resume file upload.
- **Saved answers**: work authorization, sponsorship, "how did you hear", EEO/demographics, and every
  question you've answered before. Common questions are recognized however they're worded.
- **AI drafts** for required open-ended questions ("Why do you want to work here?"), using only facts
  from your resume. They're marked "AI draft — review".
- **You, once**: questions it has never seen are asked in the terminal and saved, so you won't be asked again.
  Legal questions (work authorization, sponsorship, EEO) are never guessed.

Then you review the form, fix anything (edits you make in the browser can be saved too), solve the CAPTCHA if
there is one, and click **Submit** yourself. AutoApply never submits. Fields that still need you are
outlined in orange.

| Command | What it does |
|---|---|
| `apply [id]` | Fill the application (uses the selected job in the console) |
| `apply [id] --dry-run` | Fill it in an invisible browser, show every answer + a screenshot, submit nothing |
| `answers setup` | Answer the usual questions up front (work auth, sponsorship, EEO, how you heard…) |
| `answers list` / `answers set <key> <value>` / `answers forget <key or #>` | Manage saved answers |

First time only: `uv run playwright install chromium` (downloads the browser, ~125 MB).
Browser sign-ins persist in `~/.autoapply/browser`.

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
