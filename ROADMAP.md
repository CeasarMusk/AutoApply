# Roadmap: autofill every posting

Goal: every posting search finds can be autofilled, not just the ones on Greenhouse / Lever / Ashby.
The SimplifyJobs GitHub lists (internships + new grad) have ~8,000 active postings. This is where they live
(counted 2026-11-23):

| Site | Active postings | Status |
|---|---|---|
| Greenhouse / Lever / Ashby | ~2,000 | Full support |
| Workday | ~2,300 | Best effort: stops at sign-in (Phase 4) |
| iCIMS | ~700 | Best effort: stops at sign-in (Phase 5) |
| SmartRecruiters / Workable / Jobvite | ~400 | Best effort; Workable already fills well (Phase 3) |
| Company career sites, Oracle, Taleo, SuccessFactors, Pinpoint… | ~2,800 | Best effort (Phase 6) |

Rule for every phase: **AutoApply never clicks Submit, Create Account or sign-in, and never touches bot
checks.** It may click "Apply"-type links to reach a form. Password fields are never read or saved.

---

## Phase 1: GitHub lists as a source ✅ done

- `sources/github_lists.py` reads the SimplifyJobs `listings.json` files, keeps only active, visible postings,
  and caches them for 1h in `~/.autoapply/cache/github_lists.json`.
- Greenhouse/Lever/Ashby boards linked from the lists (~750) are added to search, so those jobs come with
  full descriptions plus every other open role at those companies.
- All other listings become jobs with `source` = `workday`, `icims`, `smartrecruiters`, `workable`, `jobvite` or `web`.
- If a linked board's API fails, its GitHub listing stands in for it. Saved jobs on that board aren't closed.
- Turn it off with `set GITHUB false`.
- Result: a remote-intern search went from 65 boards to 811 boards and ~60k jobs, with 344 matches; it takes ~35s.

## Phase 2: Site adapters + generic fallback ✅ done

- `apply/sites/`: `for_job(job)` picks an adapter by source, then by URL host. Each adapter defines
  `form_url`, `prepare` (cookie banner, click "Apply"), `enrich` and `step_signature`.
- Greenhouse / Lever / Ashby are full support. Every other site uses the generic adapter (best effort).
- `runner.wait_for_user`: while the "When you're done" prompt is up, it watches for a step, URL or new-tab
  change and re-scans and fills on its own.
- Learning across pages: answers you change on any page of a multi-page form can be saved.
- Honeypot fields ("for robots only") are dropped. When a bot check shows up, the user is told to solve it.

Dry-run results: Workable filled almost completely. Workday and iCIMS reach sign-in with the email filled.
A Gusto-hosted company page stops at Cloudflare's human check.

## Phase 3: Single-page ATS adapters (SmartRecruiters, Workable, Jobvite)

- `sites/smartrecruiters.py`, `workable.py`, `jobvite.py`: form URL rules (SmartRecruiters `…/apply`,
  Workable `…/apply/`).
- Support their custom dropdowns in `fill.py` if `_combobox_pick` misses them.
- Handle forms that parse the uploaded resume and overwrite fields: upload first, then
  `fill.wait_until_idle` (same as Lever).
- Mark them full support once 3 real dry runs each come back with no failed fields.

## Phase 4: Workday (largest share)

Every company's Workday site uses the same `data-automation-id` markup, so one adapter covers all ~2,300 postings.

- **Accounts** (`apply/accounts.py`):
  - One generated password per Workday site (host + tenant), stored in `~/.autoapply/accounts.yaml`
    (chmod 600, or the OS keyring if `keyring` is installed). The email comes from the resume.
  - Known site: fill sign-in. New site: fill the Create Account form. **The user clicks Create Account and
    handles email verification.**
  - The browser profile keeps sessions alive.
- **Flow:** Apply → "Apply Manually" (or "Autofill with Resume": upload, wait until idle, refill cleared
  fields) → My Information / My Experience / Application Questions / Voluntary Disclosures / Self Identify / Review.
  - Track the step with `data-automation-id="progressBar"`. The Phase 2 watcher re-fills each step.
- **Widgets:**
  - Dropdowns (`button[aria-haspopup=listbox]` + `promptOption`) and multi-select prompt search.
  - Dates split into MM / YYYY boxes.
  - "Add" buttons for work experience and education, filled from `MasterResume`.
- EEO and sponsorship questions follow `answers.SENSITIVE`: saved answers or ask the user, never guess.
- Needs the user present for real-site testing.

## Phase 5: iCIMS

- The form lives in a cross-origin iframe (`icims_content_iframe`), so open the iframe's `src`
  (`?in_iframe=1`) directly.
- Sign-in and account handling reuse `accounts.py` from Phase 4. Multi-step handling comes from Phase 2.
- The layout differs by company: dry-run ~5 and fix the common failures. Needs the user present.

## Phase 6: Long tail (company sites, Oracle, Taleo, SuccessFactors, Pinpoint…)

- These stay on the generic adapter. Add a dedicated adapter only when a host reaches ~100 postings in
  the lists (re-count hosts periodically).
- Optional AI fallback for labels the scanner can't map: send the field list (labels + options) to
  `LLM.parse` to map each label to a standard answer key. It never invents facts; sensitive keys still need
  saved answers.
- Known limits: hard CAPTCHAs, SSO-only portals, and applications by email or PDF won't autofill. Those
  jobs open for the user to apply manually.

---

## Testing each phase

- `uv run pytest -q`: local HTML fixtures for each adapter (no network).
- Real sites, dry runs only: `uv run autoapply apply <id> --dry-run --no-ai --no-tailor`. Check the summary
  table on 3+ postings per site.
- Steps past sign-in (Workday, iCIMS) are tested interactively with the user, who signs in and clicks Next/Submit.
