# Plan: unify `main` with `develop`, then make the repo interview-ready

Written 2026-11-11 as a handoff. It is self-contained: a fresh session can
execute it without the conversation that produced it. Work top to bottom and
stop at any step whose check fails.

## Context

The goal is to publish JARVIS on GitHub as an interview project and later open
PRs from `develop` to `main`. Two things block that today:

- **`develop` and `origin/main` share no history.** `origin/main` is a single
  commit (`9359e7f` "Initial commit") holding only the MIT `LICENSE`.
  `develop` is a linear history from a different root (`6022416`). GitHub
  refuses a PR between unrelated histories.
- **The repo lacks what a reviewer looks for first**: CI, a README that sells
  the project in one screen, screenshots, and a stated limitations list.

**Decision (2026-11-11): no rebase.** `origin/main` has nothing worth keeping
except the LICENSE file, so the file is copied into `develop` and `main` is
then pointed at `develop`'s history. This keeps every existing commit hash,
so the hashes cited in the docs stay valid. An earlier draft of this plan
rebased `develop` onto `main`; that approach is dropped.

Facts checked on 2026-11-11 (re-check them before starting):

- `origin/main` contains exactly one file, `LICENSE`, in one commit.
- `develop` has no `LICENSE` file. It has 226 commits, no merge commits, and
  is 50 commits ahead of `origin/develop` with 0 behind (a fast-forward push).
- Local `main` is a stale pointer at `938e922`, an ancestor of `develop`, with
  no upstream. `origin/test` is the same commit as `origin/main`.
- `pyproject.toml` already declares `license = "MIT"`.
- `.env` was never committed and no real secrets are in tracked files.
- `gh` is not installed; PRs are opened in the browser.
- Two files are untracked and must be committed first:
  `docs/future-improvements.md` and this file.

## Ground rules

- **No Claude or AI attribution on commits** (no `Co-Authored-By`).
- One commit per step. Before every commit these must pass:
  `make lint`, `make typecheck`, `make test`, and for `web/` changes
  `npm run lint && npm run typecheck && npm test` inside `web/`.
- This machine has no Docker. Do not run `make test-db`.
- Do not fix anything listed in `docs/future-improvements.md`; it is deferred.
- Do not open a PR.
- The only push to `main` is the single one in Part A step 5. Never
  force-push `develop`, and never rewrite its history.

## Part A — Give `main` and `develop` one history

1. **Clean tree.** On `develop`, commit the two untracked docs:
   `docs: record deferred review findings and the GitHub readiness plan`.
   Then `git status` must be clean.
2. **Bring in the licence.** `git fetch origin`, then copy the exact file from
   main so the copyright line is preserved:
   ```bash
   git checkout origin/main -- LICENSE
   ```
   Commit as `chore: add MIT license`.
3. **Check before pushing.** All must hold:
   - `git diff origin/main:LICENSE develop:LICENSE` prints nothing.
   - `git rev-list --left-right --count develop...origin/develop` shows `N 0`
     (ahead only).
   - `make lint && make typecheck && make test` pass.
4. **Push `develop`.** `git push origin develop`. It is a fast-forward; if git
   asks for a force, stop and report instead.
5. **Point `main` at the same history — confirm with Harshit before running.**
   ```bash
   git push --force-with-lease=main:9359e7f origin develop:main
   ```
   This replaces GitHub's "Initial commit" on `main`. Nothing is lost, because
   the LICENSE now lives in `develop`. If the push is rejected by branch
   protection, stop and ask him to allow force-pushes on `main` for this one
   push; do not work around it.
6. **Fix local `main`.** `git fetch origin`, then
   `git branch -f main origin/main` and
   `git branch --set-upstream-to=origin/main main`. Its old commit is still
   inside `develop`, so nothing is lost.

Result: `main` and `develop` are the same commit. Later work lands on
`develop` and reaches `main` through a normal PR at
`https://github.com/bkharshit/jarvis-agent-platform/compare/main...develop`.

`origin/test` still points at the old initial commit. Leave it alone; mention
it to Harshit as a branch he may want to delete.

## Part B — Interview-readiness

All of this lands on `develop`.

1. **CI workflow.** New `.github/workflows/ci.yml`, on push to `develop` and
   `main` and on pull requests:
   - `backend`: install `uv`, `uv sync` (check the `install` target in the
     `Makefile` for the exact extras), then `make lint`, `make typecheck`,
     `make test`. Python 3.12.
   - `integration`: a `pgvector/pgvector:pg17` service with user and password
     `jarvis` and db `jarvis`, then
     `uv run pytest tests/integration -q -m db`. Read
     `tests/integration/conftest.py` first: it creates and migrates
     `jarvis_test` itself.
   - `web`: Node LTS, `npm ci` in `web/`, then `npm run lint`,
     `npm run typecheck`, `npm test`, `npm run build`.
   The integration job cannot be proven locally. It is confirmed by the first
   run on GitHub and fixed there if it fails.
2. **README restructure.** `README.md` is 690 lines today.
   - New first screen: one-line pitch, CI and license badges, a screenshot, a
     Mermaid architecture diagram, a capability table by stage, and a
     five-command quickstart on the mock provider (no API key needed; the
     mock needs `strategy.type: function_calling`).
   - Correct the status line: every roadmap stage, F1 and S1–S14, has shipped.
   - Replace "production-grade" with wording that can be defended, such as
     "production-style architecture".
   - Move the API reference (currently the `## HTTP API` section, lines
     75–593) to a new `docs/api.md` and link it.
   - Add "Known limitations and roadmap" linking
     `docs/future-improvements.md`, a "Design decisions" section linking
     `docs/adr/` and `docs/decisions.md`, a "License" line, and a short,
     honest "How this was built" note on AI-assisted development.
3. **Screenshots.** Run `uv run jarvis serve` and the web dev server against
   the mock provider and capture four views into `docs/assets/`: agent studio
   with a streaming run, the approval pause card, the trace waterfall, and
   the workflow canvas. Reference them from the README. Use real runs only;
   no mocked data.
4. **Demo script.** New `docs/demo-script.md`: a 2–3 minute click path for
   the video Harshit records himself. Cover a streaming run, a tool approval
   pause and resume, a sub-agent trace waterfall, and a webhook-fired run.
5. **Container quickstart.** New `Dockerfile` and an `api` service in
   `compose.yaml` so a stranger can run `docker compose up`. It cannot be
   tested on this machine, so add a fourth CI job that builds the image,
   starts the stack, and curls `/v1/capabilities`. If that job cannot be made
   green quickly, drop this step and keep the `uv` quickstart only.
6. **Push** `develop` with an ordinary push. Do not push to `main`; Harshit
   opens the PR from `develop` to `main` himself.

## Not in scope

- The deferred review findings in `docs/future-improvements.md`.
- The stage deep-dive docs kept outside this repo. They stay unpublished.
- Opening the PR from `develop` to `main`.

## Verification

- **Part A:** `git rev-parse origin/main origin/develop` prints the same hash
  twice; `git ls-tree origin/main LICENSE` lists the file; the GitHub repo
  page shows the MIT licence and the full history on `main`.
- **Part B:** local gates before every commit. After the push, every CI job
  is green on GitHub and the README renders on the repo page with working
  images, badges and links.
