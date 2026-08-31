# Threat model

## Trust boundaries

The verifier, workflow, Action, project policy, AI policy, and English source are trusted only when read from the pull request's base revision. Pull request ARBs, repository API responses containing their text, cross-locale strings, model output, and provider errors are untrusted data.

The example `pull_request_target` workflow checks out the immutable base SHA. It never checks out, imports, builds, installs dependencies from, or runs scripts from the pull request head. Target ARBs are fetched as passive bytes through GitHub's Contents API with an immutable `ref` SHA.

## Credentials

The deterministic job has no AI credential. Paid review runs in the protected `translation-verification` environment and should require maintainer approval. Use a dedicated provider API key with provider-side request and monthly spending limits. Per-run limits in this verifier are defense in depth; ephemeral GitHub runners cannot enforce a race-free organization-wide daily or monthly budget.

No authorization header, API key, full provider error response, or raw model envelope is written to the report. Configure secrets through the environment variable named by `api_key_env`; do not commit credentials.

## Prompt injection

Deterministic checks block strong role/instruction/tool-call indicators before model use. The system prompt identifies every translation and context field as untrusted quoted data. Requests contain no tools, plugins, remote files, images, or browsing capabilities. Responses must match a strict JSON contract, and tool calls are rejected.

Prompt-injection detection cannot be mathematically complete. The primary control is capability isolation: even a manipulated model has no tool or repository credential and can only return an advisory verdict that is parsed as data.

## Failure semantics

Malformed translations and deterministic policy violations produce `unacceptable`. Provider outages, malformed model responses, API acquisition failures, and exhausted review limits produce `incomplete` unless a deterministic error already makes the result `unacceptable`. Unchecked required work never becomes `acceptable`.

The verifier does not approve, reject, merge, modify, or comment on a pull request. It emits a job summary, JSON artifact, and workflow annotations only.
