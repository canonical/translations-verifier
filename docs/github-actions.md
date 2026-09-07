# GitHub Actions Integration Guide

This guide explains how to integrate the **WLReviser Bot** into your GitHub repository to enable automated, AI-assisted translation verification and updates on Weblate pull requests.

The reusable workflow hosted in `canonical/translations-verifier` runs the WLReviser CLI directly within your repository's GitHub Actions environment, using your repository's tokens, secrets, and runner allocations.

---

## Overview

Once configured, maintainers can interact with the bot directly via pull request conversation comments:

```text
/translations verify
/translations apply WL-0123456789AB WL-ABCDEF012345
/translations apply all
```

- **`/translations verify`**: Compares pull request translation changes against the base commit, submits them for AI review, generates a versioned report artifact (retained for 30 days), and posts findings directly in the pull request conversation.
- **`/translations apply <ID> ...`**: Applies specific rejected translations with AI-suggested corrections directly to Weblate.
- **`/translations apply all`**: Applies all eligible rejected translations from the latest report to Weblate.

---

## Prerequisites

Before integrating the bot, ensure your project has:

1. **A Weblate project**: With translation components matching your repository's structure.
2. **An AI service endpoint**: An OpenAI-compatible Chat Completions endpoint (e.g., OpenRouter, OpenAI, or a self-hosted model).
3. **GitHub repository permissions**: You must have Admin access to your repository to configure Actions workflows, secrets, and variables.
4. **Target environment**: This integration is designed for GitHub.com repositories using GitHub-hosted Ubuntu runners (or compatible runners supporting `queue: max`).

---

## Step-by-Step Setup

### Step 1: Add the Caller Workflow

Add a workflow file at `.github/workflows/wlreviser-bot.yaml` on your repository's **default branch**.

You can copy the template from [examples/github-actions/wlreviser-bot.yaml](../examples/github-actions/wlreviser-bot.yaml):

```yaml
name: WLReviser Bot

on:
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write
  actions: write

jobs:
  translations:
    if: >-
      github.event.issue.pull_request &&
      github.event.issue.state == 'open' &&
      github.run_attempt == 1 &&
      github.event.comment.user.type == 'User' &&
      contains(fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'), github.event.comment.author_association) &&
      contains(fromJSON(vars.WLREVISER_BOT_PR_AUTHORS || '["weblate"]'), github.event.issue.user.login) &&
      (startsWith(github.event.comment.body, '/translations verify') ||
       startsWith(github.event.comment.body, '/translations apply '))
    permissions:
      contents: read
      pull-requests: write
      issues: write
      actions: write
    uses: canonical/translations-verifier/.github/workflows/wlreviser-bot.yaml@main
    with:
      allowed-pr-authors: ${{ vars.WLREVISER_BOT_PR_AUTHORS || '["weblate"]' }}
    secrets:
      WEBLATE_TOKEN: ${{ secrets.WEBLATE_TOKEN }}
      WL_BOT_AI_TOKEN: ${{ secrets.WL_BOT_AI_TOKEN }}
```

> **Versioning recommendation:** While `@main` tracks the latest changes, in production you may pin a full 40-character commit SHA (e.g., `@<commit-sha>`) for immutable versioning.

### Step 2: Add Project Translation Configuration

Add `.github/.wlreviser-bot.yaml` to your **default branch**. This file configures the translation components, Weblate mappings, and AI settings.

Example configuration:

```yaml
weblate:
  api_url: https://hosted.weblate.org/api/
  project: your-weblate-project-slug

github:
  repository: owner/repository-name

ai:
  api_url: https://openrouter.ai/api/v1/
  model: qwen/qwen-2.5-72b-instruct
  max_concurrency: 4

components:
  - weblate_component: component-slug
    source_locale: en
    context_locales: [de, fr]
    format: arb  # arb, po, or html
    source_file: path/to/source/en.arb
    path: path/to/translations/{locale}.arb
    push_after_commit: false
```

#### Existing Reference Configurations
Reference configurations for existing projects are available in the [examples](../examples/) directory:

| Repository | Reference Configuration |
| --- | --- |
| `ubuntu/app-center` | [appcenter-wlreviser-bot.yaml](../examples/appcenter-wlreviser-bot.yaml) |
| `canonical/ubuntu-desktop-provision` | [installer-wlreviser-bot.yaml](../examples/installer-wlreviser-bot.yaml) |
| `canonical/prompting-client` | [prompting-client-wlreviser-bot.yaml](../examples/prompting-client-wlreviser-bot.yaml) |
| `canonical/firmware-updater` | [firmware-updater-wlreviser-bot.yaml](../examples/firmware-updater-wlreviser-bot.yaml) |
| `canonical/desktop-security-center` | [security-center-wlreviser-bot.yaml](../examples/security-center-wlreviser-bot.yaml) |
| `canonical/ubuntu-flutter-plugins` | [ubuntu-flutter-plugins-wlreviser-bot.yaml](../examples/ubuntu-flutter-plugins-wlreviser-bot.yaml) |
| `canonical/ubuntu-pro-for-wsl` | [ubuntu-pro-for-wsl-wlreviser-bot.yaml](../examples/ubuntu-pro-for-wsl-wlreviser-bot.yaml) |

> **Note on `push_after_commit`:** Defaults to `true`. If Weblate already has automatic git push configured for its components, set `push_after_commit: false` to avoid redundant pushes.

### Step 3: Configure Repository Secrets & Variables

Navigate to **Settings → Secrets and variables → Actions** in your repository.

#### Repository Secrets
- `WEBLATE_TOKEN` *(required)*: Personal API token or bot token with read and translation-edit access to your Weblate project.
- `WL_BOT_AI_TOKEN` *(required if using an authenticated AI service)*: API key for your AI provider. Can be omitted if your endpoint does not require authentication.

> **Cross-organization note:** For repositories under external organizations (such as `ubuntu/app-center`), Canonical organization-level secrets do not propagate across organizations. Provision the secrets directly in the target repository or its organization.

#### Repository Variables (Optional)
- `WLREVISER_BOT_PR_AUTHORS` *(optional)*: JSON array of GitHub usernames allowed to trigger translation reviews on pull requests. Defaults to `'["weblate"]'`. If Weblate opens PRs under a bot account (e.g. `weblate[bot]`) or a custom user, set this variable accordingly:
  ```json
  ["weblate", "weblate[bot]"]
  ```

### Step 4: Ensure GitHub Actions Permissions

Under **Settings → Actions → General**:
1. Ensure Actions are enabled.
2. In **Workflow permissions**, ensure workflows are allowed to read repository contents.
3. If your organization restricts which actions and reusable workflows can run, allow:
   - `canonical/translations-verifier/.github/workflows/wlreviser-bot.yaml`
   - `actions/setup-python`
   - `actions/upload-artifact`

---

## Command Usage and Authorization

### Command Syntax
Comment on the main conversation of an open pull request (not on an inline code review):

- `/translations verify`
- `/translations apply WL-0123456789AB WL-ABCDEF012345`
- `/translations apply all`

**Formatting requirements:**
- Commands must be lowercase and use single space separators.
- Finding IDs must be uppercase (matching `WL-[0-9A-F]{12}`).
- Do not mix `all` with specific IDs.
- Extra text, shell syntax, or markdown formatting on the command line is rejected.

### Authorization Policy
To safeguard Weblate data and API credentials, the bot enforces strict authorization before executing:

1. **Repository Role**: The commenter's current role on the repository must be **Maintain** or **Admin**. (Write permissions alone are insufficient).
2. **Canonical Organization Membership**: The commenter must be a **public member** of the `canonical` GitHub organization.
   - *Note for team members:* Check your membership status at `https://github.com/orgs/canonical/people`. If marked "Private", switch it to "Public" so the GitHub API can verify membership without elevated organization tokens.
3. **Open Weblate Pull Request**: The PR must be currently open and authored by an allowed user (`weblate` by default).

When an authorized command is received, the bot reacts with an `:eyes:` reaction and begins execution. Unauthorized or malformed commands receive no reaction, output, or runner execution.

---

## Workflow Architecture and Behaviors

### Concurrency and Per-PR Queue
- Commands share the concurrency group `wlreviser-bot-<repository_id>-pr-<pr_number>`.
- `cancel-in-progress: false` and `queue: max` ensure commands run serially per pull request without canceling in-flight tasks.
- Different pull requests run independently and can execute concurrently.

### Reports and Artifacts
- `/translations verify` saves the JSON review report as an artifact named `translations-pr-<N>` with a 30-day retention period.
- Re-running verify safely replaces the previous artifact for that pull request across runs.
- `/translations apply` reads the latest valid report artifact for that PR. If no report is found or the report has expired:
  ```text
  No saved verification report is available for this pull request. Run /translations verify first.
  ```

### Stale State Warning
- Applying changes does not delete or refresh the saved report.
- The bot does not re-read current Weblate state before writing. If translations have been edited in Weblate since verification ran, an older report could overwrite newer edits. Generate a fresh report when in doubt.

### Token Permissions Used
The caller workflow requests four specific permissions:

| Permission | Purpose |
| --- | --- |
| `contents: read` | Read `.github/.wlreviser-bot.yaml` configuration and source translation files at the PR commits. |
| `pull-requests: write` | Access PR metadata and post comments and reactions on pull requests. |
| `issues: write` | Post comments and reactions on issues. |
| `actions: write` | Delete older verification report artifacts before uploading an updated report. |

### Output and Failure Handling
- Successful review output is posted directly into the PR conversation, split cleanly across comments without truncation.
- If a command fails, times out, or encounters an internal error, the bot posts only a generic failure message:
  ```text
  Generation failed.
  ```
  Internal logs, tracebacks, and environment variables are never posted publicly.

---

## Testing Your Integration

Once you have added the workflow and configuration files:

1. Open or locate an open pull request authored by `weblate` (or your configured author login).
2. Ensure your Canonical membership is public and you have Maintain or Admin access on the repository.
3. Post `/translations verify` as a comment on the PR.
4. Verify the following:
   - The bot adds an `:eyes:` reaction to your comment within seconds.
   - The workflow run starts in the **Actions** tab of your repository.
   - The bot posts the formatted findings summary into the PR comments upon completion.
   - A `translations-pr-<PR_NUMBER>` artifact is attached to the workflow run.
5. If findings are reported, test applying a suggestion:
   ```text
   /translations apply <FINDING_ID>
   ```
   Confirm that the bot updates the string in Weblate and reports the result in the PR conversation.
