# AutoApply

CLI that keeps one master resume, finds jobs, and helps you apply with AI-tailored
resumes and cover letters. You stay in the loop: the browser fills the form, you click Submit.

## Status
- [x] Master resume: import from PDF/DOCX/TXT, or build/edit step by step
- [x] Interactive console (`autoapply` with no arguments)
- [x] Job search: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, BambooHR, Workday, Oracle boards
- [x] Offline list of ~15,000 US internships / new-grad roles from [Keryx](https://github.com/GodlyDonuts/keryx)
- [x] Apply: fills Greenhouse / Lever / Ashby forms; you review and click Submit
- [x] Aggregator sources (Remote OK, Keryx)
- [x] Local AI via Ollama
- [ ] Fit scoring
- [x] Tailoring: one-page PDF resume per job, with preview

## Setup
```bash
uv sync
uv run autoapply init
```

**AI provider.** `init` asks which one to use; change it any time with `autoapply ai`, which lists
all three, shows whether each is set up, and lets you switch (or `ai use claude-code|api|ollama
[--model NAME]` without the menu). It's stored as `llm_backend` in `~/.autoapply/config.yaml`:
- `claude-code` (default): uses your Claude Pro/Max subscription through Claude Code's
  headless mode (`claude -p`). Install Claude Code and run `claude` once to log in. Calls count
  toward your normal subscription usage limits. Personal use only.
- `api`: uses the Anthropic API (`export ANTHROPIC_API_KEY=...`), billed per token. Use this if you
  ever share the tool with other people.
- `ollama`: a model running locally in [Ollama](https://ollama.com): free and nothing leaves your
  machine, but slower and less capable than Claude. `ollama pull qwen3:14b` (or any model), then
  `ai use ollama --model qwen3:14b`. Tailoring, essays, resume
  import and suggestions all use it, with the same fact guards (a rewrite that adds numbers or terms
  not in your master resume is still rejected). Settings: `OLLAMA_HOST` (default `$OLLAMA_HOST` or
  `localhost:11434`), `OLLAMA_CTX` (context window, default 16384; raise it for long postings if
  your machine has the memory), `OLLAMA_THINK` (let thinking models reason first: better, slower).
  Scanned (image-only) PDFs can't be imported with a local model.

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

It searches ~60 built-in tech companies (Greenhouse / Lever / Ashby boards), the Remote OK feed and
Keryx's offline internship / new-grad list (see below), and ranks results by how well they match your
resume (titles, skills and level). "Jobs like my resume" makes one AI call to build a search profile,
cached until the resume changes.

### Keryx: internships and new-grad roles
[Keryx](https://github.com/GodlyDonuts/keryx) (MIT) tracks ~3,500 company boards and republishes
the open US internship / new-grad roles every 15 minutes. AutoApply uses it in three layers:

1. **Offline:** a snapshot ships inside the package (`src/autoapply/sources/data/`), so search works
   with no network at all.
2. **Synced:** `keryx sync` downloads the current list from GitHub (~9 MB, skipped when unchanged via
   ETag, never more than once per 15 minutes). Searches do this automatically when the copy is older
   than `KERYX_SYNC` hours (default 12; `0` = only when you run it).
3. **Live:** with `KERYX_LIVE` on, a search also asks the boards behind its Keryx matches directly
   (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, BambooHR, Workday, Oracle), so you see
   postings minutes old and drop ones that closed. At most `KERYX_MAX` boards per search (default 40).

Being polite to those APIs: requests to the same host (or shared Workday/Oracle cluster) are spaced
and limited to 2 at a time, a `429`/`503` is retried once after its `Retry-After` (or the board is
skipped), and any board fetched in the last `CACHE` minutes (default 15) is read from
`~/.autoapply/cache` instead. Workday and Oracle boards are searched with your keywords (a few pages)
rather than downloaded whole. Listings that arrive without a description (Keryx, Workday) get it
fetched once, when you open, tailor or apply to the job.

Only Greenhouse, Lever and Ashby forms are auto-filled; for other boards `apply` opens the posting in
your browser.

| Command | What it does |
|---|---|
| `search` | Menu search (above) |
| `search --last` | Repeat the last search |
| `search "data engineer" --level entry -l US` | Search without the menu |
| `jobs [-s saved] [-g text] [--remote] [--all]` | Saved jobs, best match first |
| `use <id>` → `info` / `open` / `save` / `ignore`, `back` | Work with one job |
| `profile [--refresh]` | What your resume says to search for |
| `companies add <slug>` | Add a company beyond the built-in list (Greenhouse / Lever / Ashby slug) |
| `companies add <careers-board link>` | Same, for any supported board, e.g. `https://acme.wd5.myworkdayjobs.com/External` |
| `keryx status` / `keryx sync [--force]` | How fresh the offline list is / update it now |
| `set STARTER false` / `set REMOTEOK false` / `set KERYX false` | Turn off the built-in list / Remote OK / Keryx |
| `set KERYX_LIVE false` | Search Keryx offline only (no live board checks) |
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
| `apply [id] --no-tailor` | Upload your original resume instead of a tailored one |
| `tailor [id] [--preview] [--redo]` | Make a one-page resume tailored to the job; `--preview` opens the PDF |
| `answers setup` | Answer the usual questions up front (work auth, sponsorship, EEO, how you heard…) |
| `answers list` / `answers set <key> <value>` / `answers forget <key or #>` | Manage saved answers |

### Apply queue

Apply to a whole list of matches one after another:

```
autoapply > search
autoapply > queue build          # asks: which jobs (last search / shortlist / all) and which order
autoapply > queue run
```

`queue build --from last|saved|all --order match|recent` skips the questions. `match` puts the best
resume match first; `recent` puts the most recently posted first. Optional `--min-match 60` and
`--limit 20`; the same role posted in several cities is queued once, and jobs you've applied to,
ignored, or that closed are left out. `queue run` takes each job in turn: apply now, read the
description, skip for now, remove, ignore, or stop. Every form still opens in the browser for you to
review and submit; applied jobs leave the queue, everything else stays for next time. Jobs on sites
AutoApply can't fill are opened in your browser, and you're asked whether you applied.

| Command | What it does |
|---|---|
| `queue` / `queue show` | The queue, in order |
| `queue order match\|recent` | Re-sort it |
| `queue add <id…>` / `queue remove <id…>` / `queue clear` | Change what's in it |
| `queue run [--max N] [--no-tailor] [--no-ai]` | Work through it |

First time only: `uv run playwright install chromium` (downloads the browser, ~125 MB).
**Tailored resumes.** Before filling a form, Claude picks and orders the experience, projects,
bullets and skills from your master resume that fit the job, and may reword bullets and the
summary in the job's language. A guard rejects any rewrite that adds a number, tool or name
not already in your master resume (your original wording is kept instead), and the layout
tightens or drops the lowest-priority lines to fit one page. You then choose: use it, preview
it, tailor again, or upload your original. Each job's PDF is saved in
`~/.autoapply/out/<id>-<company>/` and reused next time. Turn it off with `set TAILOR false`.

Browser sign-ins persist in `~/.autoapply/browser`.

## Resume commands
| Command | What it does |
|---|---|
| `autoapply resume import resume.pdf` | Claude reads the file into `~/.autoapply/master_resume.yaml`; review, fix, save |
| `autoapply resume build` | Step-by-step wizard (with optional AI bullet suggestions) |
| `autoapply resume edit [section]` | Menu editor: add/edit/reorder/delete items and bullets |
| `autoapply resume show` | Print it with problems highlighted |
| `autoapply resume github <profile link>` | Add projects from your public repos (see below) |
| `autoapply resume transcript file.pdf` | Save a transcript; it's uploaded when a form asks for one |
| `autoapply resume validate` | Check after hand-editing the YAML |
| `autoapply usage` | Token usage per task |

Every item gets a stable id (`exp2`, `exp2.b3`, …). Tailoring selects items by id, so
it can't invent experience you don't have. Put *more* in the master than fits on a page.

**Projects from GitHub.** `resume github https://github.com/you` lists your public repos (most
starred first, forks skipped unless `--forks`), takes a summary and any "Features" list from each
README, and lets you tick which ones become projects. It can also add your GitHub link and any
languages your skills section is missing. The default (`--basic`) is quick: one request per repo, no AI.
`--deep` also reads each repo's language breakdown and dependency files (package.json, pyproject.toml,
Cargo.toml, go.mod…) to name the frameworks used, and has the AI write 2-3 bullets from the README;
any bullet with a number or name that isn't in the repo's own text is dropped (`--no-ai` skips the
bullets). Anonymous GitHub access allows 60 requests an hour; set `GITHUB_TOKEN` for more.

Data lives in `~/.autoapply` (override with `AUTOAPPLY_HOME`).

## Tests
```bash
uv run pytest
```
