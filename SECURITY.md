# Security

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through
GitHub: [Security → Report a vulnerability](https://github.com/Nitin23123/GhostPatch/security/advisories/new).

Include what you did, what happened, and the version (`ghostpatch --version`). You'll get a reply
within a few days. Fixes go into the next release, and the report is credited in the changelog
unless you'd rather not be named.

## Supported versions

Security fixes are made to the latest release. Please update (`pipx upgrade ghostpatch`) before
reporting.

## How GhostPatch protects you

GhostPatch lets an AI model change code and run your tests, so it is built on the assumption that
the model, or the text it reads (an issue, a stack trace, a file), may try to do harm.

- **Commands need approval.** In the default `ask` mode every command waits for you. In `safe`
  mode only recognised test and read-only git commands run unasked, and not with options that
  reach outside the project (writing files elsewhere, other settings files, `..` or absolute
  paths). `all` is for throwaway environments such as CI.
- **Files.** The agent can only read and write inside the project, never inside `.git/` or
  `.ghostpatch/`, never `.env` files, and it doesn't follow links out of the project. `undo`
  ignores run records whose paths point elsewhere.
- **Secrets.** Commands and test runs start without API keys or GitHub tokens in their
  environment. Secret values are replaced by `[redacted]` in run history, shared pages, pull
  requests, commit messages and issue comments. A project's `.env` can't change where your key
  is sent (`GHOSTPATCH_BASE_URL`) or turn approvals off (`GHOSTPATCH_APPROVE`).
- **The dashboard** listens only on 127.0.0.1, checks the Host header (no DNS rebinding),
  requires a custom header on every change (no cross-site requests) and can't be framed by
  another site.
- **GitHub.** Only comments by a repository's owners, members and collaborators reach the agent.
  The Action removes the token `actions/checkout` stores in the repository before the agent
  runs, and pushes with its own token through the GitHub CLI. Labelling an issue is your go-ahead,
  so label only issues you have read.

## What it can't protect against

Running tests runs code: your project's, and tests the agent wrote. `safe` mode is not a
sandbox. For a repository you don't trust, use `ask`, or run GhostPatch in a container or CI.
