# GitHub Actions Setup

The reusable workflow in `canonical/translations-verifier` runs the bundled WLReviser CLI in the
consuming repository's Actions context. It uses that repository's token, secrets, artifacts,
and runner allocation. No changes to WLReviser's command-line interface are required.

This integration targets GitHub.com and GitHub-hosted Ubuntu runners. It uses GitHub.com's
`$/` self-repository references and `queue: max`; it is not a GitHub Enterprise Server template.

## Publish the Shared Workflow

Publish this project, including [.github/workflows/wlreviser-bot.yaml](../.github/workflows/wlreviser-bot.yaml)
and [actions/wlreviser-bot/action.yml](../actions/wlreviser-bot/action.yml), to the public
`canonical/translations-verifier` repository. Record the full 40-character commit SHA containing the
implementation. Consumers must pin this SHA; do not use a mutable branch reference.

The reusable workflow's `$/actions/wlreviser-bot` reference loads the action at that same
commit. The action installs the CLI from its own bundled repository, not from PyPI, the caller's
checkout, or a PR branch. Updating the consumer's one pinned SHA updates the workflow, helper,
and CLI together. Third-party actions are also pinned to full commit SHAs.

## Configure Each Consumer

1. Add the [caller template](../examples/github-actions/wlreviser-bot.yaml) to the consuming
   repository's default branch at `.github/workflows/wlreviser-bot.yaml`. Replace
   `FULL_RELEASE_SHA` with the published bot commit SHA.
2. Add the matching configuration below at `.github/.wlreviser-bot.yaml` on that same branch.
   Preserve the CLI configuration schema; do not add workflow settings or credentials to it.
3. Set the repository Actions secret `WEBLATE_TOKEN` with read and translation-edit access to
   the configured Weblate project. Set `WL_BOT_AI_TOKEN` for authenticated AI endpoints;
   the supplied OpenRouter examples require it. The GitHub token is provided automatically.
4. Enable Actions and allow the reusable workflow and its pinned `actions/setup-python` and
   `actions/upload-artifact` dependencies under the repository and organization Actions policies.
   The workflow explicitly requests write permissions for comments and artifact replacement.
5. Confirm the actual Weblate PR author login. The default is `weblate`. To change it, set the
   repository Actions variable `WLREVISER_BOT_PR_AUTHORS` to a JSON array such as
   `["weblate", "another-service[bot]"]`.

| Consumer | Configuration |
| --- | --- |
| `ubuntu/app-center` | [appcenter-wlreviser-bot.yaml](../examples/appcenter-wlreviser-bot.yaml) |
| `canonical/ubuntu-desktop-provision` | [installer-wlreviser-bot.yaml](../examples/installer-wlreviser-bot.yaml) |
| `canonical/prompting-client` | [prompting-client-wlreviser-bot.yaml](../examples/prompting-client-wlreviser-bot.yaml) |
| `canonical/firmware-updater` | [firmware-updater-wlreviser-bot.yaml](../examples/firmware-updater-wlreviser-bot.yaml) |
| `canonical/desktop-security-center` | [security-center-wlreviser-bot.yaml](../examples/security-center-wlreviser-bot.yaml) |

Provision secrets separately for `ubuntu/app-center`: Canonical organization secrets do not
automatically cross into the `ubuntu` organization. The template explicitly maps the two secrets
instead of using `secrets: inherit`. Organization secrets for Canonical consumers must allow those
repositories. Use credentials scoped to the intended Weblate project and AI service.

The example configurations retain `push_after_commit: false`. Weblate automatic push must already
be configured for their components. Choose `true` in a project's configuration only when explicit
WLReviser-triggered pushes are intended.

## Commands and Authorization

Post one of these as a new comment in the PR's main conversation, not an inline review comment:

```text
/translations verify
/translations apply WSN-0123456789AB WSN-ABCDEF012345
/translations apply all
```

Use single spaces, lowercase command names, and uppercase finding IDs. `all` is case-insensitive
and must appear alone. A trailing newline is accepted; additional prose, shell syntax, flags,
or command lines are not. Edited comments are not new requests.

Only human commenters whose current repository role is exactly **Maintain** or **Admin**, and
whose membership in `canonical` is **public**, can run commands. Write permission is insufficient.
Private Canonical members are denied because this integration deliberately does not request an
organization-membership token. Members can make their membership public in GitHub's organization
People settings if appropriate under their organization's policy.

The action checks the original comment author's live role, public membership, and unchanged
comment body after leaving the queue. The PR must still be open and authored by an allowed account.
Event author association is only a preliminary filter, not proof of authorization. Denied requests
receive no accepted reaction or public authorization details and never install or invoke the CLI.
Accepted requests receive an `eyes` reaction before setup and execution.

Job-level filters prevent ordinary unrelated comments from allocating a runner. GitHub can still
record skipped workflow runs. A plausible command needs a runner for exact parsing and live API
authorization; malformed commands that pass the preliminary prefix filter stop there.

## Token Permissions

| Permission | Use |
| --- | --- |
| `contents: read` | Default-branch configuration and translation-file reads |
| `pull-requests: read` | PR metadata and changed-file listing |
| `issues: write` | Conversation comments and reactions |
| `actions: write` | Delete earlier runs' reports before uploading a replacement |

There is no PR write permission, Git ref lock, Contents write permission, or additional GitHub
credential. `issues: write` is not a comment-only scope. Actions write is required for cross-run
artifact deletion, not for native concurrency or ordinary upload. GitHub offers no
artifact-delete-only token scope. The shared worker has Actions write for both commands, although
apply itself only performs Actions reads. Permissions apply only to the relevant caller/worker jobs.

## Reports and Concurrency

Verification saves the unchanged schema-version-2 JSON as `translations-pr-N.json` in one artifact
named `translations-pr-N` in the **consumer** repository. Retention is 30 days, or less if an operator
deletes it. Reports contain translation content and AI reasoning. In public repositories, anyone
with the GitHub access needed to download Actions artifacts can read them; do not include confidential
translations in public workflows.

Before uploading a valid replacement, the action deletes all artifacts with that exact reserved
PR-specific name, including artifacts from previous workflow runs. Do not use these reserved names
in other workflows. `upload-artifact` overwrite alone cannot replace artifacts across runs.
Failed verification preserves the earlier report. Replacement is not atomic: if deletion succeeds
and upload fails, no saved report remains. Run verify again to recover.

Apply uses the latest eligible report when its queued job starts. It validates expiration, age,
producer workflow/run provenance, archive contents, schema version, and repository/PR identity.
The CLI then performs its existing configuration/report compatibility checks. The artifact must
come from a successful `issue_comment` run of the same caller workflow on the default branch.
While GitHub finalizes a producer run, it is eligible only if all its jobs have already completed
successfully. This lets the next queued apply use a just-finished verification safely.
Renaming that workflow requires a fresh verification. Reports and downloaded archives are limited
to 20 MiB; archives must contain only the expected JSON file, not paths, symlinks, or extra files.

Missing, expired, or untrusted reports produce:

```text
No saved verification report is available for this pull request. Run /translations verify first.
```

Apply does not delete or refresh the report, so further selections remain possible within its
original retention period. It does not run an implicit verify or compare current PR head/config
SHAs. **A retained report can overwrite newer Weblate edits.** A queued verify can supersede findings
before a later queued apply runs, including changing what `apply all` selects.

Both commands share the concurrency group `wlreviser-bot-REPOSITORY_ID-pr-PR_NUMBER`.
Only one worker per PR runs at a time, including authorization, artifact operations, publication,
and cleanup. Different PRs may run concurrently. `cancel-in-progress: false` preserves active work;
`queue: max` permits up to 100 waiting jobs. Excess jobs are canceled by GitHub. Queue order follows
when jobs start waiting, not necessarily comment creation time. Do not add another concurrency
lock with this group to the caller workflow.

## Output and Failures

Successful CLI stdout is posted as escaped literal text, split across comments without truncation.
Known credential values are redacted. Verification responses link to the JSON artifact; both commands
link to their workflow run. Exit zero can include item-level update, commit, push, or verification
errors; inspect the complete output. These are not treated as command crashes.

A nonzero CLI exit, crash, timeout, or authorized setup/storage failure publishes only
`Generation failed.` rather than captured stdout, stderr, or a stack trace. The wrapper does not
stream CLI output into Actions logs or upload logs. Temporary configuration, reports, output,
and the virtual environment are cleaned up at the end. GitHub outages, runner loss, hard job
timeouts, and manual cancellation can prevent delivery or cleanup.

The CLI has a 50-minute subprocess timeout inside a 60-minute worker timeout. No entire apply
invocation is automatically retried: writes may already have happened. Actions UI reruns are
ignored to prevent accidental replay. Inspect Weblate after an interrupted apply and issue a new
command comment deliberately. Canceling a workflow does not roll back Weblate changes.

## Validation and Rollout

CI runs pytest, Ruff, Pyright (including the action-only helper), actionlint, and the composite
action metadata schema check. actionlint 1.7.12 does not yet recognize `queue: max` or `$/` action
references. CI excludes only those two exact diagnostics; `test_action_workflow_contract` checks
their exact values, single lock placement, same-revision action path, and token permissions.
Remove these exclusions when upgrading to a linter that understands the documented GitHub syntax.

Local checks cannot prove GitHub's hosted scheduling, token policy, or cross-repository loading.
Before production rollout, use a sandbox PR and disposable Weblate component to verify:

- An unrelated comment allocates no runner, and Write-only/private-member/nonmember requests are denied.
- A public Canonical Maintain/Admin member can verify an allowed open Weblate PR, including a fork PR.
- The `$/` reference loads the pinned bot commit and all API calls work with exactly the listed scopes.
- A second verify replaces the first run's artifact, leaving one JSON report with 30-day retention.
- Explicit IDs and `all` apply from the saved report; missing/expired reports and crashes have safe replies.
- Three queued commands on one PR run serially, while another PR can execute concurrently.
- Config/helper changes on a PR branch never control privileged execution, and large output remains complete.

Roll out to one consumer first, then the remaining four. Publishing the shared commit, setting
remote secrets/variables, and merging consumer workflow/config changes are deployment steps;
creating these files locally does not perform them.