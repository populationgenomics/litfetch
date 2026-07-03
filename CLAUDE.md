# litfetch development notes

litfetch resolves a scholarly article identifier to its retrievable files and to
the markdown derived from them. See [`CONTEXT.md`](CONTEXT.md) for the domain
model and language, and [`README.md`](README.md) for usage.

## Working norms

Operating directives for Claude (and any agent) in this repo; they counteract default
model dispositions.

- **Resist the minimal-diff reflex.** Don't reach for the smallest change that hides the
  symptom (special-casing, papering over root causes). Aim for the correct fix at the
  right complexity level — not the smallest, not gold-plated.
- **Fail loudly and early.** Raise on a missing expected input or precondition; never fall
  back to a default/placeholder to limp along. A placeholder is an explicit caller input,
  never a code default.
- **Push back; don't just comply.** When a design, name, or approach seems worse —
  including a shortcut you're asked to take — say so with reasoning, unprompted. The
  author owns the final call.
- **Offer better alternatives with trade-offs.** When a materially better approach than
  the proposed one exists, present it and the trade-offs — don't just execute the ask.
- **Investigate before producing.** Read the code and verify constraints first. Don't
  treat a training-pattern convention as load-bearing unchecked; don't speculate about
  what you can read.
- **Explain non-obvious changes first.** For a change whose rationale isn't self-evident,
  give the why before showing or applying the diff.
- **Ask when unsure** rather than assume intent.
- **No intensifiers or emphasis filler.** Drop words and phrases that add emphasis but no
  information — "that's the key", "crucially", "importantly", "the key insight", "it's
  worth noting". State the point plainly. Applies to all prose: chat replies, PR/review
  comments, commit messages, and docs.

## Code style

@docs/style/general.md
@docs/style/python.md

## Docs

The primary audience for docs is a model reading them as context; humans second. Be
terse: state each decision, mechanism, and rationale once — no rhetorical emphasis, no
persuasion, no recaps. Every token written is re-paid on every future read.

## Committing

- **Stage explicit paths**, not `git add -A` / `.`.
- **Pre-commit is the full static-check gate** (`.pre-commit-config.yaml`): lint, format,
  hygiene, and pyright. CI runs the same hooks via `pre-commit run --all-files`, so the
  two can't drift. Ensure hooks are installed (`pre-commit install`) — if not, install or
  ask the author; never bypass with `--no-verify`.
- **Correct a pushed branch with a new commit on top**, not amend + force-push. PRs
  squash-merge, so `main` history stays linear regardless and intermediate fixups vanish
  on merge. Reserve force-push for rebasing a branch onto `main`.

## CI and review

- **Pin third-party GitHub Actions to the latest stable release**: the moving major tag
  (`@v3`) where the action publishes one, else the exact latest version (`@v8.2.0`). Verify
  against the action's releases when adding or bumping one.
