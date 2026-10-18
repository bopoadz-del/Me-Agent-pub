# Contributing

## Commit identity

This is a public repository: commit metadata is as public as the code. No
commit may carry a person's name or personal email as author or committer.

- Commit with a role identity and a no-reply address, for example
  `git -c user.name=<role> -c user.email=noreply@<domain> commit ...`.
  A local merge or rebase writes commits too, so it needs the same identity.
- Bots and CI jobs that write commits take their identity from the
  environment (a role name and a no-reply address), never from a personal
  profile. Today no workflow in `.github/workflows/` writes commits.

## Merging

- **Do not use GitHub's merge, squash or rebase buttons** (or `gh pr merge`).
  GitHub stamps the merging account's profile name and email on the commit
  it creates.
- Merge locally instead: bring the branch up to date with `main`, squash or
  rebase it with the role identity, and push `main` as an ordinary
  fast-forward push. If `main` now holds the PR's own head commit, GitHub
  marks the PR merged; otherwise close it with a link to the commit on
  `main`.
- Never force-push `main`.

## Changes

- One change per PR, with tests that fail before the change and pass after
  it (red first). CI must be green on the PR head before it is merged.
- No case written for one kit, document, model or test: a rule must hold the
  same for every domain kit. Kit-specific values live in the kit's sheet
  (`domain-kits/sheets/`) and in test data only.
- Each PR is graded by someone who did not write it.
- No secrets, tokens, connection strings or personal data in any file.
