import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import yaml

HELPER = Path(__file__).parents[1] / "actions" / "wlreviser-bot" / "runner.py"
SPEC = importlib.util.spec_from_file_location("translations_action", HELPER)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot
SPEC.loader.exec_module(bot)


@pytest.mark.parametrize(
    ("body", "verb", "selectors"),
    [
        ("/translations verify", "verify", ()),
        ("/translations verify\r\n", "verify", ()),
        ("/translations apply ALL", "apply", ("all",)),
        (
            "/translations apply WSN-0123456789AB WSN-ABCDEF012345",
            "apply",
            ("WSN-0123456789AB", "WSN-ABCDEF012345"),
        ),
    ],
)
def test_action_command(body: str, verb: str, selectors: tuple[str, ...]) -> None:
    command = bot.parse_command(body)
    assert (command.verb, command.selectors) == (verb, selectors)


@pytest.mark.parametrize(
    "body",
    [
        "hello",
        "/translations",
        "/translations apply",
        "/translations verify extra",
        "/translations apply all WSN-0123456789AB",
        "/translations apply --help",
        "/translations apply $(id)",
        "/translations verify\nprose",
        "> /translations verify",
        " /translations verify",
        "/translations apply wsn-0123456789ab",
    ],
)
def test_action_command_denied(body: str) -> None:
    with pytest.raises(bot.Denied):
        bot.parse_command(body)


@pytest.fixture
def event() -> dict[str, Any]:
    return {
        "action": "created",
        "repository": {"full_name": "canonical/repo"},
        "issue": {"number": 7, "state": "open", "pull_request": {}},
        "comment": {
            "id": 123,
            "body": "/translations verify",
            "author_association": "MEMBER",
            "user": {"id": 42, "login": "maintainer", "type": "User"},
        },
    }


class FakeGitHub(bot.GitHub):
    def __init__(self, event: dict[str, Any], role: str = "maintain", member: int = 204) -> None:
        bot.GitHub.__init__(self, "canonical/repo", "secret")
        self.event = event
        self.role = role
        self.member = member
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        self.calls.append((method, path, data))
        if path.endswith("/permission"):
            result = {"role_name": self.role, "permission": "write"}
        elif "/public_members/" in path:
            return self.member, b"", {}
        elif path.endswith("/pulls/7"):
            result = {
                "state": "open",
                "user": {"login": "weblate"},
                "base": {"repo": {"full_name": "canonical/repo"}},
            }
        elif path.endswith("/issues/comments/123"):
            result = self.event["comment"]
        else:
            raise AssertionError(path)
        return 200, json.dumps(result).encode(), {}


@pytest.mark.parametrize("role", ["maintain", "admin"])
def test_action_authorization(event: dict[str, Any], role: str) -> None:
    api = FakeGitHub(event, role)
    assert bot.authorize(api, event, ["weblate"], "issue_comment", "1").verb == "verify"
    assert all(method == "GET" for method, _path, _data in api.calls)


@pytest.mark.parametrize("role", ["write", "read", "triage", "custom-maintainer", ""])
def test_action_denies_other_roles(event: dict[str, Any], role: str) -> None:
    api = FakeGitHub(event, role)
    with pytest.raises(bot.Denied):
        bot.authorize(api, event, ["weblate"], "issue_comment", "1")
    assert not any("public_members" in path for _method, path, _data in api.calls)


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_action_membership_fails_closed(event: dict[str, Any], status: int) -> None:
    with pytest.raises(bot.Denied):
        bot.authorize(FakeGitHub(event, member=status), event, ["weblate"], "issue_comment", "1")


def test_action_rerun_never_calls_api(event: dict[str, Any]) -> None:
    api = FakeGitHub(event)
    with pytest.raises(bot.Denied):
        bot.authorize(api, event, ["weblate"], "issue_comment", "2")
    assert not api.calls


@pytest.fixture
def report() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "repository": "canonical/repo",
        "pull_request_number": 7,
        "pull_request_url": "https://github.com/canonical/repo/pull/7",
        "created_at": datetime.now(UTC).isoformat(),
        "head_sha": "old-head",
    }


def archive_report(
    content: bytes, filename: str = "translations-pr-7.json", mode: int = 0
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(filename)
        info.external_attr = mode << 16
        archive.writestr(info, content)
    return buffer.getvalue()


def test_action_report_preserves_stale_head(report: dict[str, Any]) -> None:
    content = json.dumps(report).encode()
    bot.validate_report(content, "canonical/repo", 7, datetime.now(UTC))
    assert bot.extract_report(archive_report(content), "translations-pr-7.json") == content


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", 1),
        ("repository", "attacker/repo"),
        ("pull_request_number", 8),
        ("pull_request_url", "https://github.com/canonical/repo/pull/8"),
        ("created_at", "2000-01-01T00:00:00Z"),
        ("created_at", "2100-01-01T00:00:00Z"),
    ],
)
def test_action_rejects_invalid_report(report: dict[str, Any], key: str, value: Any) -> None:
    report[key] = value
    with pytest.raises(bot.BotError):
        bot.validate_report(json.dumps(report).encode(), "canonical/repo", 7, datetime.now(UTC))


@pytest.mark.parametrize(
    ("filename", "mode"),
    [
        ("../translations-pr-7.json", 0),
        ("/translations-pr-7.json", 0),
        ("translations-pr-7.json", stat.S_IFLNK | 0o777),
        ("extra.json", 0),
    ],
)
def test_action_rejects_unsafe_archive(filename: str, mode: int) -> None:
    with pytest.raises(bot.BotError):
        bot.extract_report(archive_report(b"{}", filename, mode), "translations-pr-7.json")


def test_action_output_is_complete_and_literal() -> None:
    import html

    text = "<script>&```\n@maintainer \U0001f600" * 10000
    bodies = bot.comment_bodies(text, 123, "run")
    assert len(bodies) > 1
    assert all(len(body.encode()) < 60000 for body in bodies)
    assert (
        "".join(html.unescape(body.split("<pre>", 1)[1].split("</pre>", 1)[0]) for body in bodies)
        == text
    )


def test_action_apply_environment_is_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in bot.TOKEN_NAMES:
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("GITHUB_OUTPUT", "/runner/output")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("UNRELATED_SECRET", "unrelated")
    environment = bot.child_environment("apply")
    assert environment["WEBLATE_TOKEN"] == "secret"
    for name in [
        "GITHUB_TOKEN",
        "WL_BOT_AI_TOKEN",
        "GITHUB_OUTPUT",
        "ACTIONS_RUNTIME_TOKEN",
        "UNRELATED_SECRET",
    ]:
        assert name not in environment


def test_action_redacts_success_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBLATE_TOKEN", "sensitive")
    assert bot.redact("sensitive error") == "[REDACTED] error"


@pytest.mark.parametrize("outcome", [2, -9, "timeout", "launch"])
def test_action_execution_failure_is_not_publishable(
    tmp_path: Path, event: dict[str, Any], monkeypatch: pytest.MonkeyPatch, outcome: int | str
) -> None:
    bot.save_state(
        tmp_path, {"authorized": True, "verb": "apply", "number": 7, "selectors": ["all"]}
    )
    monkeypatch.setenv("WEBLATE_TOKEN", "secret")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command[1:] == [
            "apply",
            str(tmp_path / "config.yaml"),
            str(tmp_path / "translations-pr-7.json"),
            "all",
        ]
        assert kwargs["shell"] is False
        kwargs["stdout"].write(b"partial output")
        kwargs["stderr"].write(b"Traceback: secret")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 3000)
        if outcome == "launch":
            raise OSError("secret")
        assert isinstance(outcome, int)
        return subprocess.CompletedProcess(command, outcome)

    monkeypatch.setattr(bot.subprocess, "run", run)
    with pytest.raises((bot.BotError, subprocess.TimeoutExpired, OSError)):
        bot.execute(FakeGitHub(event), tmp_path)
    assert not bot.load_state(tmp_path).get("succeeded")
    assert not (tmp_path / "output.txt").exists()


def test_action_report_age_boundary() -> None:
    now = datetime.now(UTC)
    assert bot.recent((now - timedelta(days=29)).isoformat(), now)
    assert not bot.recent((now - timedelta(days=30)).isoformat(), now)


def test_action_workflow_contract() -> None:
    root = HELPER.parents[2]
    workflow = yaml.load(
        (root / ".github/workflows/wlreviser-bot.yaml").read_text(), Loader=yaml.BaseLoader
    )
    caller = yaml.load(
        (root / "examples/github-actions/wlreviser-bot.yaml").read_text(), Loader=yaml.BaseLoader
    )
    action = yaml.load((HELPER.parent / "action.yml").read_text(), Loader=yaml.BaseLoader)
    worker = workflow["jobs"]["translations"]
    assert worker["concurrency"] == {
        "group": "wlreviser-bot-${{ github.repository_id }}-pr-${{ github.event.issue.number }}",
        "cancel-in-progress": "false",
        "queue": "max",
    }
    assert "concurrency" not in workflow
    assert "concurrency" not in caller
    assert "concurrency" not in caller["jobs"]["translations"]
    assert worker["steps"][0]["uses"] == "$/actions/wlreviser-bot"
    assert (root / worker["steps"][0]["uses"].removeprefix("$/") / "action.yml").is_file()
    assert caller["on"] == {"issue_comment": {"types": ["created"]}}
    permissions = {
        "contents": "read",
        "pull-requests": "read",
        "issues": "write",
        "actions": "write",
    }
    assert worker["permissions"] == caller["jobs"]["translations"]["permissions"] == permissions
    for job in [worker, caller["jobs"]["translations"]]:
        guard = job["if"]
        for required in [
            "github.event.issue.pull_request",
            "github.event.comment.user.type == 'User'",
            "github.event.comment.author_association",
            "github.run_attempt == 1",
            "startsWith(github.event.comment.body, '/translations verify')",
            "startsWith(github.event.comment.body, '/translations apply ')",
        ]:
            assert required in guard
    steps = action["runs"]["steps"]
    assert action["runs"]["using"] == "composite"
    assert steps[0]["id"] == "preflight"
    assert steps[-2]["if"] == steps[-1]["if"] == "always()"
    for step in steps:
        if "uses" in step:
            assert "checkout" not in step["uses"]
            assert len(step["uses"].split("@")[1]) == 40
        if step.get("id") == "upload":
            assert step["with"]["retention-days"] == "30"
            assert step["with"]["if-no-files-found"] == "error"
            assert step["with"]["path"] == "${{ steps.execute.outputs.report_path }}"
        assert "github.event.comment.body" not in step.get("run", "")


class LifecycleGitHub(FakeGitHub):
    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(event)
        self.saved: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.runs: dict[int, dict[str, Any]] = {}
        self.fail_delete = False
        self.fail_config = False
        self.lose_post_response = False

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        route = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(route.query)
        endpoint = route.path.removeprefix(self.prefix)
        result: Any
        if endpoint == "":
            result = {"default_branch": "main"}
        elif endpoint == "/commits/main":
            result = {"sha": "a" * 40}
        elif endpoint == "/contents/.github/.wlreviser-bot.yaml":
            assert query == {"ref": ["a" * 40]}
            if self.fail_config:
                return 500, b"private diagnostics", {}
            result = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(b"trusted config").decode(),
            }
        elif endpoint == "/issues/comments/123/reactions":
            assert data == {"content": "eyes"}
            result = {}
        elif endpoint == "/actions/artifacts":
            start = (int(query["page"][0]) - 1) * 100
            result = {"artifacts": self.saved[start : start + 100]}
        elif endpoint.startswith("/actions/artifacts/") and method == "DELETE":
            if self.fail_delete:
                return 500, b"private diagnostics", {}
            artifact_id = int(endpoint.rsplit("/", 1)[1])
            self.saved = [artifact for artifact in self.saved if artifact["id"] != artifact_id]
            result = {}
        elif endpoint.startswith("/actions/runs/"):
            result = self.runs[int(endpoint.rsplit("/", 1)[1])]
        elif endpoint == "/issues/7/comments":
            if method == "POST":
                assert data is not None
                self.comments.append({"user": {"login": "github-actions[bot]"}, **data})
                if self.lose_post_response:
                    self.lose_post_response = False
                    raise OSError("lost response")
            start = (int(query.get("page", ["1"])[0]) - 1) * 100
            result = self.comments[start : start + 100]
        else:
            return super().request(method, path, data)
        self.calls.append((method, path, data))
        return 200, json.dumps(result).encode(), {}


@pytest.fixture
def action_environment(
    event: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    values = {
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_RUN_ID": "500",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "canonical/repo",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_WORKFLOW_REF": (
            "canonical/repo/.github/workflows/wlreviser-bot.yaml@refs/heads/main"
        ),
        "GITHUB_TOKEN": "gh-secret",
        "WEBLATE_TOKEN": "wl-secret",
        "WL_BOT_AI_TOKEN": "ai-secret",
        "ALLOWED_PR_AUTHORS": '["weblate"]',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return bot.work_directory()


def saved_artifact(artifact_id: int = 1) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": artifact_id,
        "name": "translations-pr-7",
        "expired": False,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "workflow_run": {"id": 400},
    }


def producer_run() -> dict[str, Any]:
    return {
        "event": "issue_comment",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/wlreviser-bot.yaml",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "head_repository": {"full_name": "canonical/repo"},
        "repository": {"full_name": "canonical/repo"},
    }


def test_action_verify_replaces_cross_run_report_and_publishes(
    event: dict[str, Any],
    report: dict[str, Any],
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = LifecycleGitHub(event)
    api.saved = [saved_artifact(), {**saved_artifact(2), "name": "translations-pr-8"}]
    bot.preflight(api, action_environment)
    assert (action_environment / "config.yaml").read_bytes() == b"trusted config"
    assert stat.S_IMODE(action_environment.stat().st_mode) == 0o700
    invocations = 0

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal invocations
        invocations += 1
        assert command[1] == "verify"
        assert command[3] == "https://github.com/canonical/repo/pull/7"
        assert kwargs["env"]["GITHUB_TOKEN"] == "gh-secret"
        assert kwargs["env"]["WL_BOT_AI_TOKEN"] == "ai-secret"
        assert "GITHUB_ACTIONS" not in kwargs["env"]
        Path(command[4]).write_text(json.dumps(report))
        kwargs["stdout"].write(b"Completed with item errors. wl-secret")
        kwargs["stderr"].write(b"not published")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bot.subprocess, "run", run)
    bot.execute(api, action_environment)
    assert [artifact["id"] for artifact in api.saved] == [2]
    api.saved.append(saved_artifact(3))
    monkeypatch.setenv("BOT_ARTIFACT_ID", "3")
    monkeypatch.setenv("BOT_JOB_STATUS", "success")
    bot.publish(api, action_environment)
    bot.publish(api, action_environment)
    assert invocations == 1
    assert len(api.comments) == 1
    assert "Completed with item errors. [REDACTED]" in api.comments[0]["body"]
    assert "not published" not in api.comments[0]["body"]
    assert "/artifacts/3" in api.comments[0]["body"]


def test_action_cleanup_paginates_before_deleting(event: dict[str, Any]) -> None:
    api = LifecycleGitHub(event)
    api.saved = [saved_artifact(index) for index in range(205)]
    api.saved.append({**saved_artifact(999), "name": "translations-pr-8"})
    bot.delete_reports(api, 7)
    assert [entry["id"] for entry in api.saved] == [999]
    assert sum(method == "DELETE" for method, _path, _data in api.calls) == 205


@pytest.mark.parametrize("missing", [True, False])
def test_action_apply_loads_original_report_without_extending_retention(
    event: dict[str, Any],
    report: dict[str, Any],
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    event["comment"]["body"] = "/translations apply WSN-0123456789AB"
    (action_environment.parent / "event.json").write_text(json.dumps(event))
    report["created_at"] = (datetime.now(UTC) - timedelta(days=29)).isoformat()
    api = LifecycleGitHub(event)
    api.saved = [] if missing else [saved_artifact()]
    api.runs[400] = producer_run()
    original = copy.deepcopy(api.saved)
    content = json.dumps(report).encode()

    def download(_api: Any, _artifact: dict[str, Any]) -> bytes:
        return archive_report(content)

    monkeypatch.setattr(bot, "download_archive", download)
    bot.preflight(api, action_environment)
    if missing:
        bot.publish(api, action_environment)
        assert api.comments[0]["body"].endswith(bot.MISSING_REPORT)
        assert not (action_environment / "venv").exists()
    else:
        path = action_environment / "translations-pr-7.json"
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            assert command[1:] == [
                "apply",
                str(action_environment / "config.yaml"),
                str(path),
                "WSN-0123456789AB",
            ]
            assert "GITHUB_TOKEN" not in kwargs["env"]
            kwargs["stdout"].write(b"Updated 1")
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(bot.subprocess, "run", run)
        bot.execute(api, action_environment)
        monkeypatch.setenv("BOT_JOB_STATUS", "success")
        bot.publish(api, action_environment)
        assert "Updated 1" in api.comments[0]["body"]
    assert api.saved == original
    assert not any(method == "DELETE" for method, _path, _data in api.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("head_branch", "untrusted"),
        ("path", ".github/workflows/other.yaml"),
        ("head_repository", {"full_name": "fork/repo"}),
        ("repository", {"full_name": "elsewhere/repo"}),
    ],
)
def test_action_rejects_untrusted_producer(event: dict[str, Any], field: str, value: Any) -> None:
    api = LifecycleGitHub(event)
    api.saved = [saved_artifact()]
    api.runs[400] = {**producer_run(), field: value}
    with pytest.raises(bot.MissingReport):
        bot.select_report(
            api,
            {"number": 7, "workflow_path": producer_run()["path"], "default_branch": "main"},
            datetime.now(UTC),
        )


@pytest.mark.parametrize("expired", ["flag", "timestamp", "age"])
def test_action_expired_artifact(event: dict[str, Any], expired: str) -> None:
    api = LifecycleGitHub(event)
    artifact = saved_artifact()
    if expired == "flag":
        artifact["expired"] = True
    else:
        key = "expires_at" if expired == "timestamp" else "created_at"
        artifact[key] = "2000-01-01T00:00:00Z"
    api.saved = [artifact]
    with pytest.raises(bot.MissingReport):
        bot.select_report(api, {"number": 7}, datetime.now(UTC))


def test_action_edited_comment_denied(event: dict[str, Any]) -> None:
    original = copy.deepcopy(event)
    event["comment"]["body"] = "/translations apply all"
    with pytest.raises(bot.Denied):
        bot.authorize(FakeGitHub(event), original, ["weblate"], "issue_comment", "1")


def test_action_config_failure_publishes_only_generic_message(
    event: dict[str, Any],
    action_environment: Path,
) -> None:
    api = LifecycleGitHub(event)
    api.fail_config = True
    with pytest.raises(bot.BotError):
        bot.preflight(api, action_environment)
    bot.publish(api, action_environment)
    assert api.comments[0]["body"] == "<!-- wlreviser-bot:123:status -->\nGeneration failed."


def test_action_upload_failure_never_publishes_success(
    event: dict[str, Any],
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = LifecycleGitHub(event)
    bot.preflight(api, action_environment)
    state = bot.load_state(action_environment)
    state["succeeded"] = True
    bot.save_state(action_environment, state)
    (action_environment / "output.txt").write_text("sensitive stdout")
    monkeypatch.setenv("BOT_JOB_STATUS", "failure")
    bot.publish(api, action_environment)
    assert api.comments[0]["body"].endswith(bot.FAILURE)
    assert "sensitive" not in api.comments[0]["body"]


def test_action_post_retry_does_not_duplicate(event: dict[str, Any]) -> None:
    api = LifecycleGitHub(event)
    api.lose_post_response = True
    bot.post_once(api, 7, ["one", "two"])
    assert [comment["body"] for comment in api.comments] == ["one", "two"]


def test_action_top_level_crash_is_generic(
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def crash(_api: Any, _directory: Path) -> None:
        raise RuntimeError("private exception containing a token")

    monkeypatch.setattr(bot, "preflight", crash)
    monkeypatch.setattr(sys, "argv", [str(HELPER), "preflight"])
    assert bot.main() == 1
    output = capsys.readouterr()
    assert output.out == "Generation failed.\n"
    assert output.err == ""


def test_action_cleanup_removes_private_data(
    event: dict[str, Any],
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot.preflight(LifecycleGitHub(event), action_environment)
    monkeypatch.setattr(sys, "argv", [str(HELPER), "cleanup"])
    assert bot.main() == 0
    assert not action_environment.exists()


@pytest.mark.parametrize("status", ["completed", "in_progress", "queued", "empty", "failure"])
def test_action_producer_run_finalization(
    event: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    api = LifecycleGitHub(event)

    def jobs(path: str, key: str | None = None) -> list[dict[str, str]]:
        assert path == "/repos/canonical/repo/actions/runs/400/jobs?filter=latest"
        assert key == "jobs"
        if status == "empty":
            return []
        return [
            {
                "status": "completed" if status == "failure" else status,
                "conclusion": "failure" if status == "failure" else "success",
            }
        ]

    monkeypatch.setattr(api, "pages", jobs)
    run = {**producer_run(), "status": "in_progress", "conclusion": None}
    assert bot.producer_succeeded(api, run, 400) is (status == "completed")


@pytest.mark.skipif(
    os.environ.get("WLREVISER_BOT_INSTALL_TEST") != "1",
    reason="Opt-in packaging smoke test installs dependencies from PyPI",
)
def test_action_installs_bundled_cli(tmp_path: Path) -> None:
    bot.save_state(tmp_path, {"authorized": True})
    bot.install(tmp_path)
    for arguments in [["--help"], ["verify", "--help"], ["apply", "--help"]]:
        result = subprocess.run(
            [str(tmp_path / "venv/bin/wlreviser"), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert "Usage:" in result.stdout


def test_action_download_redirect_never_forwards_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = archive_report(b"{}")
    requests: list[urllib.request.Request] = []

    class Opener:
        def open(self, request: urllib.request.Request, timeout: int) -> io.BytesIO:
            assert timeout == 60
            requests.append(request)
            if request.full_url.startswith(bot.API_URL):
                headers = Message()
                headers["Location"] = "https://storage.example/report.zip?signed=value"
                raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, io.BytesIO())
            return io.BytesIO(content)

    def opener(*handlers: Any) -> Opener:
        assert len(handlers) == 1
        assert isinstance(handlers[0], bot.NoRedirect)
        return Opener()

    monkeypatch.setattr(bot.urllib.request, "build_opener", opener)
    artifact = {"id": 1, "digest": f"sha256:{hashlib.sha256(content).hexdigest()}"}
    assert bot.download_archive(bot.GitHub("canonical/repo", "secret"), artifact) == content
    assert requests[0].get_header("Authorization") == "Bearer secret"
    assert requests[1].get_header("Authorization") is None
    assert "secret" not in requests[1].full_url
    assert (
        bot.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None
    )


@pytest.mark.parametrize("status", [404, 410])
def test_action_download_missing_report(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    api = bot.GitHub("canonical/repo", "secret")

    def request(_method: str, _path: str) -> tuple[int, bytes, dict[str, str]]:
        return status, b"private message", {}

    monkeypatch.setattr(api, "request", request)
    with pytest.raises(bot.MissingReport):
        bot.download_archive(api, {"id": 1})


@pytest.mark.parametrize("failure", ["cli", "delete", "report_secret"])
def test_action_failed_verify_preserves_previous_report(
    event: dict[str, Any],
    report: dict[str, Any],
    action_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    api = LifecycleGitHub(event)
    api.saved = [saved_artifact()]
    previous = copy.deepcopy(api.saved)
    api.fail_delete = failure == "delete"
    bot.preflight(api, action_environment)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "report_secret":
            report["warnings"] = ["wl-secret"]
        Path(command[-1]).write_text(json.dumps(report))
        kwargs["stdout"].write(b"unpublishable output")
        return subprocess.CompletedProcess(command, 2 if failure == "cli" else 0)

    monkeypatch.setattr(bot.subprocess, "run", run)
    with pytest.raises(bot.BotError):
        bot.execute(api, action_environment)
    bot.publish(api, action_environment)
    assert api.saved == previous
    assert not bot.load_state(action_environment).get("succeeded")
    assert api.comments[0]["body"].endswith(bot.FAILURE)


@pytest.mark.parametrize("body", ["/translations\tverify", "/translations  verify"])
def test_action_parser_matches_workflow_whitespace_contract(body: str) -> None:
    with pytest.raises(bot.Denied):
        bot.parse_command(body)
