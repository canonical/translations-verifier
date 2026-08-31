# Translation Verifier

`translations-verifier` performs advisory review of changed ARB translations in a GitHub
pull request. It compares each target change with the target file at the base SHA and the
English message at that same trusted SHA. Deterministic structural and security checks run
before optional semantic review through an OpenAI-compatible API.

The verifier never approves, rejects, comments on, or modifies a pull request. It writes a
JSON report, a Markdown job summary, and optional GitHub workflow annotations.

## Install and run

Python 3.11 through 3.14 and [uv](https://docs.astral.sh/uv/) are supported.

```bash
uv sync --locked
uv run translations-verifier config validate \
	--project-config examples/project.yaml \
	--ai-config examples/ai.yaml

GITHUB_TOKEN=... TRANSLATION_VERIFIER_API_KEY=... \
uv run translations-verifier verify \
	--project-config examples/project.yaml \
	--ai-config examples/ai.yaml \
	--pr https://github.com/canonical/ubuntu-desktop-provision/pull/1521 \
	--output translation-verification.json
```

For the free deterministic pass, omit the AI config and credential:

```bash
GITHUB_TOKEN=... uv run translations-verifier verify \
	--project-config examples/project.yaml \
	--pr 1521 \
	--deterministic-only
```

`GITHUB_TOKEN` is optional for public repositories but avoids the low anonymous API rate
limit. It needs read-only repository and pull-request access.

## Configuration

See the [configuration reference](docs/configuration.md) for every supported parameter,
including whether it is required, its default and accepted range, its exact runtime effect,
interactions with other fields, and focused YAML examples.

The [project example](examples/project.yaml) shows two translation roots like those in
`canonical/ubuntu-desktop-provision`. Each translation set has a repository-relative
directory, a filename pattern containing exactly one `{locale}`, an English source locale,
optional locale filters, and bounded base-branch context locales.

Project policy controls custom variable regexes, immutable terms, URL and command-flag
preservation, whitespace and Unicode handling, markup allowlists, and hard file/key/byte
limits. Unknown fields, invalid regexes, unsafe paths, and unbounded values are rejected.

The [AI example](examples/ai.yaml) configures an OpenAI-compatible base endpoint, model,
the *name* of an environment variable containing the credential, timeouts, retries, response
format fallback, and hard per-key/per-run limits. Literal credentials are not accepted in the
configuration schema.

- OpenAI: use an endpoint such as `https://api.openai.com/v1`.
- OpenRouter: use `https://openrouter.ai/api/v1` and an OpenRouter model identifier.
- Ollama: use a local endpoint such as `http://localhost:11434/v1`. Local HTTP is accepted;
	non-local HTTP requires the explicit `allow_insecure_http: true` override.

The client uses non-streaming `/chat/completions`. It first requests strict JSON Schema and
can retry with JSON-object mode for compatible providers that reject schema mode.

## Checks

The deterministic pipeline rejects malformed UTF-8/JSON, duplicate keys, invalid ARB
metadata, required key deletion, unknown source keys, invalid ICU MessageFormat, placeholder
or selector changes, printf/custom variables, URL/flag/immutable-token mutations, disallowed
or changed markup, significant whitespace changes, ANSI and unsafe Unicode controls, and
strong prompt-injection indicators.

Only clean inserted or changed messages can reach AI review. The model assesses meaning,
intent, tone, grammar, policy/legal/security semantics, and abusive or nefarious additions.
When it rejects a translation, it must provide one complete suggested replacement in the
target language. Suggestions remain advisory and are included in the JSON report, Markdown
summary, and GitHub annotation; they are never written back to the ARB file automatically.
The model has no tools, plugins, browsing, remote files, or image inputs. Its output is parsed
as untrusted data against a strict contract.

Stable exit codes and report statuses are:

| Exit | Status | Meaning |
|---:|---|---|
| `0` | `acceptable` | All required enabled checks completed without findings. |
| `1` | `unacceptable` | At least one deterministic or AI error finding exists. |
| `2` | `incomplete` | Required work could not complete because of limits, API failure, or invalid model output. |

In `--deterministic-only` mode, AI review is intentionally not required, so a clean
deterministic result is `acceptable`.

## GitHub Actions

Developers integrating the verifier into another repository should follow
[Using the verifier from another repository](docs/consumer-integration.md). It covers the
consumer file layout, project and AI configuration, Action inputs and outputs, read-only
permissions, `GITHUB_TOKEN`, protected-environment secrets, deterministic-only usage, local
testing, upgrades, common integration failures, and Weblate-specific PR and AI-spend limits.

The repository includes a composite [Action](action.yml) and a hardened
[example workflow](examples/translation-verifier.yml). Copy the example configuration into
the target repository, adapt its translation sets, and replace
`FULL_40_CHARACTER_COMMIT_SHA` with a reviewed verifier commit. The example workflow deliberately:

- runs on `pull_request_target` while checking out only the immutable base SHA;
- fetches pull-request ARBs through the GitHub API as passive bytes;
- runs a secret-free deterministic job first;
- places the paid job in a `translation-verification` environment; and
- pins every third-party Action to a full commit SHA.

Configure that environment with required maintainer reviewers and add the
`TRANSLATION_VERIFIER_API_KEY` environment secret. Use a dedicated provider key/project with
provider-side request and monthly credit limits. Ephemeral runners cannot provide a durable,
race-free daily or monthly organization quota; verifier limits are per-run defense in depth.

Never alter the workflow to check out the pull-request head, execute repository code from it,
or load verifier policy from it in a job that has secrets. Do not use a self-hosted runner for
public untrusted pull requests.

## Development

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --cov=translations_verifier --cov-report=term-missing
uv build
```

The test suite mocks GitHub and model APIs; it never calls a paid model. See
[docs/threat-model.md](docs/threat-model.md) for trust boundaries and residual risks.

## Limitations

Version 1 supports ARB files and GitHub pull requests only. Source-locale changes, renamed
ARB files, automatic translation repair, glossary services, direct Weblate API integration,
sticky comments, SARIF, and pull-request review submission are intentionally out of scope.
Prompt-injection detection is heuristic; isolation and absence of model capabilities are the
primary controls. Before public release, select a project license and complete dependency
license review, including `pyicumessageformat`.
