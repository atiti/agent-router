# Routine approvals

Codex reviews every command that has to leave the sandbox unless an allow rule covers it. In
practice most of those requests are routine reads: pull request status, the work tracker,
AgentRoute's own state. Each review adds a round trip without changing the outcome.

`agentroute approvals install` writes a managed block of allow rules into
`$CODEX_HOME/rules/default.rules`. Rules outside the block are left untouched, repeating the
command is safe, and new sessions pick the rules up.

```sh
agentroute approvals install
agentroute approvals install --extra "python3 /Users/me/workspace/tools/scripts/work_packets.py"
agentroute approvals status
agentroute approvals uninstall
```

## What is allowed

| Group | Commands |
|---|---|
| GitHub reads | `gh pr view/checks/list/diff/status`, `gh run view/list`, `gh issue view/list/status`, `gh repo view`, `gh release view/list`, `gh workflow view/list`, `gh search`, `gh auth status` |
| GitHub REST reads | `agentroute gh-get PATH [--jq EXPR] [--paginate]`, which always sends `GET` |
| AgentRoute reads | `history`, `why`, `version`, `analytics`, `audit-report`, `stats`, `models`, `classifier-status`, `approvals status/explain`, `herdr list` |
| Waits | `agentroute wait pr/run/file/timer/event/list/show/cancel` |
| Team ledger | `agentroute herdr team status/messages/inbox/inbox-ack/ack/send`, also through the venv Python |
| Output filters | `head`, `tail`, `wc`, `cut`, `jq`, `grep` |

Everything else stays reviewed, including `gh api`, merges, comments, workflow dispatch, pushes,
`agentroute wake`, `wait url`, `wait command`, `wait fire`, and launching or cleaning up workers.
Filters that can write a file or run a program (`sort`, `sed`, `awk`, `rg`, `xargs`, `tee`) are
left out on purpose.

`--extra` adds one exact prefix for a tool of your own. A bare interpreter is refused: `python3`
must be followed by an existing absolute script path, so the rule cannot run arbitrary code.

## Write commands so the rules can match

Codex only matches a command against rules when every word is literal. One construct anywhere
in the line sends the whole line to review:

| Construct | Example | Write instead |
|---|---|---|
| `~` | `python3 ~/workspace/tools/x.py` | the absolute path |
| Redirection | `gh pr view 179 2>&1` | drop the redirection |
| Variables, `$(...)` | `gh pr view $PR` | the literal value |
| Unquoted `# * ? [ ] { } ^ \` | `wait pr owner/repo#179` | `'owner/repo#179'` or `owner/repo 179` |
| Environment prefix | `GH_PAGER=cat gh pr view 179` | drop the prefix |
| Unlisted segment | `gh pr view 179 \| python3 -c ...` | use `--jq`, or a listed filter |

Pipes, `&&`, `||` and `;` are fine when every segment is covered. To check a line:

```sh
agentroute approvals explain "gh pr checks 179 | head -20"
```

It reports whether the line runs without review and names the construct to change.
