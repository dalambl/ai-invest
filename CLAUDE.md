# CLAUDE.md

## Maintainer

The primary maintainer of this repo is Claude (the AI agent). Continuously clean up the codebase, improve documentation, add tooling for efficiency, and adjust processes as necessary. The repo state — code, notes, and this file — is the project's memory. Keep it accurate and useful.

## Background

This repo is designed to create a financial system for tracking my portfolio, suggest trades, manage risks, stress-test portfolio etc. It might grow in scope.

This is an experimental/research application, not a library.

### Development commands

* Add dependency: `uv add <package> [--dev]`. Use `--dev` for development-only dependencies.
* Lint: `uv run ruff check [--fix]`
* Format: `uv run ruff format`
* Run the application: `uv run main`
* **Important:** Always run all checks before committing: `uv run ruff format && uv run ruff check --fix && uv run ty check`
* Use `uv run ...` to run python commands in the environment. Do not call `python3` directly.

### Commit practices

* Make atomic commits — one logical change per commit.
* Always run all checks before committing (see above).

## Code style

* Write code that explains itself rather than needs comments.
* Simplicity is paramount. Always look for ways to simplify, use existing utilities and approaches in the codebase rather than creating new code, and identify and suggest architectural improvements.
* Don't write error handling code unless asked, nor smooth over exceptions/errors unless they are expected as part of control flow. Write code that will raise an exception early if something isn't expected. Enforce important expectations with asserts.
* Add only extremely minimal code comments and no docstrings unless asked, but don't remove existing comments.
  * Add comments only when doing things out of the ordinary, to highlight gotchas, or if less clear code is required due to an optimization.
* Use Python 3.13+ features.
* Follow ruff format.

## Project layout

```
notes/                       # Experiment notes and documentation
  notes/data/                # Non-text experiment artifacts (data files)
  notes/plots/               # Plot images and visualizations
```

## Note-taking requirements

This project involves many experiments. We maintain structured notes to track progress:

* **`notes/overview.md`** is a running high-level index that points to specific detailed notes files. Always begin by reading this file and any linked notes relevant to the current task.
* Each non-trivial change, experiment, or development push should result in:
  1. A new or expanded `notes/NNN-<topic>.md` file documenting what was done, decisions made, and results.
  2. An update to `notes/overview.md`: add a table entry linking to the new/updated notes file, update the current status section to reflect the project's latest state, and update the plan to reflect any decisions made in this session.
* Notes files are numbered sequentially (001, 002, ...) for easy ordering.
* When expanding an existing notes file, keep its current number — do not create a new number. Update the summary in the overview table to reflect the new content; do not add a new table row.
* Keep notes concise but sufficient to understand what happened and why.
* Do not include "next steps" in notes unless that particular piece of work is unfinished. Notes document what was done, not what to do next.
* The overview table includes a summary column capturing key takeaways/changes, and a current status section above the table summarizing the project's state.

### Experiment artifacts

* Non-text artifacts (data files, plots, images) go in `notes/data/` or `notes/plots/` and are referenced from the relevant notes files.
* Checking in notes and artifacts is the primary way to record experiment results.
* **Embed images directly in notes files** using `![description](../plots/filename.png)` markdown syntax. Every figure created during an experiment should be embedded in the corresponding `notes/NNN-<topic>.md` file so the full experiment is reviewable inline without opening files separately.

## Keeping this file updated

Update this CLAUDE.md whenever:
* New tools or dependencies are added.
* Development commands or workflows change.
* Code style practices are adjusted.
* Project structure evolves.
* New conventions or practices are established.

This file should always reflect the current state of the project.