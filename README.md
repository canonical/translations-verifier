# WLReviser

WLReviser verifies translation changes in a GitHub pull request with an
OpenAI-compatible model, records the result in a versioned JSON report, and can apply selected
corrections to Weblate. Repository files are read at the pull request's immutable base and head
commit SHAs, including heads from forks.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- GitHub and Weblate accounts with access to the configured project
- An OpenAI-compatible Chat Completions endpoint that supports strict JSON Schema responses

Install the development environment and inspect the CLI:

```console
$ uv sync
$ uv run wlreviser --help
```

To install the command in an isolated uv-managed environment instead:

```console
$ uv tool install .
$ wlreviser --help
```

## Configuration

Start with [the Ubuntu Desktop Provision example](examples/ubuntu-desktop-provision.yaml). The
YAML configuration is strict: unknown keys, ambiguous component mappings, malformed repository
paths, and credentials embedded in endpoint URLs are rejected.

```yaml
weblate:
	api_url: https://hosted.weblate.org/api/
	project: ubuntu-desktop-translations

github:
	repository: canonical/ubuntu-desktop-provision

ai:
	api_url: http://localhost:11434/v1/
	model: qwen3
	max_concurrency: 4

components:
	- weblate_component: provision-common
		source_locale: en
		context_locales: [de, fr]
		format: arb
		source_file: packages/ubuntu_provision/lib/src/l10n/ubuntu_provision_en.arb
		path: packages/ubuntu_provision/lib/src/l10n/ubuntu_provision_{locale}.arb
		push_after_commit: false
```

Endpoint URLs must use HTTPS, except for `localhost`, `127.0.0.1`, and `::1`. They cannot contain
credentials, query strings, or fragments. `max_concurrency` defaults to `4` and accepts values
from `1` through `32`.

Each component has these fields:

| Field | Meaning |
| --- | --- |
| `weblate_component` | Weblate component slug. It must be unique in the file. |
| `source_locale` | Locale used by the concrete `source_file`. |
| `context_locales` | Optional peer locales supplied to the reviewer for context. |
| `format` | One of `arb`, `po`, or `html`. |
| `source_file` | Normalized repository-relative source path with no placeholders. |
| `path` | Target path template containing exactly one `{locale}` placeholder. |
| `push_after_commit` | Whether WLReviser requests a push after committing. Defaults to `true`; set to `false` when automatic push is enabled for the component in Weblate. |

Locale values refer to filenames, so regional values such as `zh_TW` and `en_US` are preserved.
WLReviser resolves the corresponding Weblate translation by exact normalized filename. Configure
source translation files, not generated outputs such as Dart localization files.

### Supported Formats

| Format | Comparison unit | Automatic apply |
| --- | --- | --- |
| ARB | Each non-metadata string property; `@key` data is review metadata | Yes |
 | PO | Each non-obsolete `msgid` and `msgctxt` identity, including plural forms | Single-form units only |
| HTML | The complete file as one unit | No |

PO support is for monolingual catalogs. Bilingual PO layouts are not supported. Metadata-only
changes do not produce findings. Added and removed units are checked against the base source
document before AI review. Multi-form PO units are reviewed, but a rejected unit is not
automatically applyable because one scalar suggestion cannot safely replace a complete plural set.

## Credentials

Credentials are read only from the process environment and must not be placed in YAML:

| Variable | Used by | Requirement |
| --- | --- | --- |
| `GITHUB_TOKEN` | `verify` | Required; needs read access to the pull request and repository contents. |
| `WEBLATE_TOKEN` | `verify`, `apply` | Required; verification reads mappings and apply writes units and operates on repositories. |
| `WL_BOT_AI_TOKEN` | `verify` | Optional; omit it for a local endpoint that does not require authentication. |

WLReviser does not serialize these values into reports or intentionally print them in errors. Treat
the environment of the WLReviser process as a credential boundary.

## Verify

Set the required environment variables, then run:

```console
$ uv run wlreviser verify CONFIG.yaml https://github.com/OWNER/REPOSITORY/pull/NUMBER REPORT.json
```

For example:

```console
$ uv run wlreviser verify examples/ubuntu-desktop-provision.yaml \
		https://github.com/canonical/ubuntu-desktop-provision/pull/1521 \
		wlreviser-report.json
```

The pull request URL must have exactly the form shown and must match `github.repository`. WLReviser
captures the base and head SHAs, paginates all changed files, and reads configured translation
content at those immutable commits. Each value-only change is reviewed against its source,
previous target, format metadata, and any available peer-locale context. AI requests run with the
configured concurrency limit and do not expose tools to the model. WLReviser applies a fixed review
checklist and sends `temperature: 0` and `seed: 0` to minimize variation between identical reviews.
Provider implementations and model updates can still affect results, so exact reproducibility and
complete discovery of every semantic issue cannot be guaranteed by a single AI pass.

AI review payloads contain the format, target locale, source and proposed text, prior translation,
relevant translation metadata, explicit gettext context, and configured peer translations. File
paths, translation keys, component names, Weblate URLs and unit IDs, and report IDs remain local to
WLReviser and are not sent to the model. Empty optional fields are omitted from the request.

The terminal shows aggregate counts and compact rejected findings. Full HTML documents and HTML
suggestions are deliberately omitted from terminal output; they remain available in the report.

### Reports

Reports use schema version `2` and include creation time, pull request coordinates and SHAs,
Weblate coordinates, finding IDs, reviewed strings, model reasons, one scalar suggested
translation per rejected finding, mapping data, and per-item errors. They are written atomically
with mode `0600`, and apply rejects unsupported or internally inconsistent report versions.

Reports contain translation content that may be sensitive. Store and transmit them accordingly,
even though credentials are excluded.

## Apply

Apply one or more rejected findings by report ID:

```console
$ uv run wlreviser apply CONFIG.yaml REPORT.json WL-0123456789AB WL-ABCDEF012345
```

Use `all` by itself to select every applyable rejection:

```console
$ uv run wlreviser apply CONFIG.yaml REPORT.json all
```

WLReviser first verifies that the report's repository, Weblate project, components, formats, and
paths match the current configuration. It then updates each selected Weblate unit directly with
the report suggestion and sets Weblate state `20`. HTML findings require manual correction and
are never applyable.

**Apply intentionally performs no stale-state check.** It does not reread the current Weblate
target before writing, so an old report can overwrite a newer edit. Generate a fresh report when
that risk is unacceptable.

An item failure does not stop later items. After unit updates, WLReviser requests a commit for each
component that had at least one successful update. It then independently requests a push unless
that component sets `push_after_commit: false`, in which case WLReviser relies on Weblate's automatic
push configuration. Components without successful updates are not committed or pushed.

## Retries and Exit Status

GitHub, AI, and Weblate operations retry a transient transport or HTTP failure exactly once.
`Retry-After` is honored when the service supplies it. There is no unbounded SDK retry layer.

A completed `verify` exits `0` even when the report contains rejected or errored items. A completed
`apply` also exits `0` when individual updates, commits, or pushes fail; inspect its summary for
those outcomes. Configuration, credential, report, or command-level service failures exit `2`.

## Security Boundaries

- Translation content and metadata are sent to the configured AI endpoint. Choose that endpoint
	according to the data's confidentiality requirements.
- AI output must satisfy a strict local schema and semantic validation before entering a report.
- Pull request data, AI text, and service errors are rendered as plain terminal text rather than
	interpreted as Rich markup.
- The model cannot browse or call tools through WLReviser.
- Applying a report is a write operation with the stale-state behavior described above.

## GitHub Actions

Use the [GitHub Actions setup guide](docs/github-actions.md) to enable `/translations verify`
and `/translations apply` in other repositories through `canonical/translations-verifier`.
It includes the reusable workflow, consumer template, permissions, secrets, report retention,
and per-PR command queue. The CLI itself remains independent of GitHub Actions.

## Development

Run the complete local checks with:

```console
$ uv run pytest
$ uv run ruff check .
$ uv run pyright
```
