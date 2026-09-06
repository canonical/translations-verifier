from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

API_URL = "https://api.github.com"
MAX_BYTES = 20 * 1024 * 1024
FINDING_ID = re.compile(r"WL-[0-9A-F]{12}")
FAILURE = "Generation failed."
MISSING_REPORT = (
    "No saved verification report is available for this pull request. "
    "Run /translations verify first."
)
TOKEN_NAMES = ("GITHUB_TOKEN", "WEBLATE_TOKEN", "WL_BOT_AI_TOKEN")


class BotError(Exception):
    pass


class Denied(BotError):
    pass


class MissingReport(BotError):
    pass


@dataclass(frozen=True)
class Command:
    verb: str
    selectors: tuple[str, ...] = ()


def parse_command(body: str) -> Command:
    body = body.rstrip("\r\n")
    if "\n" in body or "\r" in body:
        raise Denied()
    parts = body.split(" ")
    if parts == ["/translations", "verify"]:
        return Command("verify")
    if len(parts) < 3 or parts[:2] != ["/translations", "apply"]:
        raise Denied()
    selectors = parts[2:]
    if len(selectors) == 1 and selectors[0].lower() == "all":
        return Command("apply", ("all",))
    if not all(FINDING_ID.fullmatch(selector) for selector in selectors):
        raise Denied()
    return Command("apply", tuple(selectors))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    @property
    def prefix(self) -> str:
        return f"/repos/{self.repository}"

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        if not path.startswith("/") or path.startswith("//"):
            raise BotError()
        request = urllib.request.Request(
            API_URL + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "wlreviser-bot",
            },
            method=method,
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise BotError()
            return response.code, content, dict(response.headers.items())

    def get(self, path: str) -> dict[str, Any]:
        status, content, _headers = self.request("GET", path)
        if status != 200:
            raise BotError()
        value = json.loads(content)
        if not isinstance(value, dict):
            raise BotError()
        return cast(dict[str, Any], value)

    def mutate(self, method: str, path: str, data: dict[str, Any] | None = None) -> None:
        status, _content, _headers = self.request(method, path, data)
        if status not in {200, 201, 204}:
            raise BotError()

    def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            status, content, _headers = self.request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if status != 200:
                raise BotError()
            value = json.loads(content)
            raw_entries = value[key] if key else value
            if not isinstance(raw_entries, list):
                raise BotError()
            entries = cast(list[Any], raw_entries)
            if not all(isinstance(item, dict) for item in entries):
                raise BotError()
            result.extend(entries)
            if len(entries) < 100:
                return result
        raise BotError()


def authorize(
    api: GitHub,
    event: dict[str, Any],
    allowed_authors: list[str],
    event_name: str,
    run_attempt: str,
) -> Command:
    if event_name != "issue_comment" or event.get("action") != "created" or run_attempt != "1":
        raise Denied()
    issue = event["issue"]
    comment = event["comment"]
    if (
        "pull_request" not in issue
        or issue["state"] != "open"
        or comment["user"]["type"] != "User"
        or comment["author_association"] not in {"OWNER", "MEMBER", "COLLABORATOR"}
        or event["repository"]["full_name"] != api.repository
    ):
        raise Denied()
    command = parse_command(comment["body"])
    login = comment["user"]["login"]
    if not re.fullmatch(r"[A-Za-z0-9-]+", login):
        raise Denied()
    live = api.get(f"{api.prefix}/issues/comments/{int(comment['id'])}")
    if (
        live["id"] != comment["id"]
        or live["user"]["id"] != comment["user"]["id"]
        or live["user"]["login"] != login
        or live["body"] != comment["body"]
    ):
        raise Denied()
    role = api.get(f"{api.prefix}/collaborators/{login}/permission")
    if role.get("role_name") not in {"maintain", "admin"}:
        raise Denied()
    status, _content, _headers = api.request("GET", f"/orgs/canonical/public_members/{login}")
    if status != 204:
        raise Denied()
    pull = api.get(f"{api.prefix}/pulls/{int(issue['number'])}")
    if (
        pull["state"] != "open"
        or pull["user"]["login"].lower() not in {author.lower() for author in allowed_authors}
        or pull["base"]["repo"]["full_name"] != api.repository
    ):
        raise Denied()
    return command


def private_write(path: Path, content: bytes) -> None:
    with path.open("wb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def load_state(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "state.json").read_text(encoding="utf-8"))


def save_state(directory: Path, state: dict[str, Any]) -> None:
    temporary = directory / "state.tmp"
    private_write(temporary, json.dumps(state).encode())
    temporary.replace(directory / "state.json")


def set_output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value or not re.fullmatch(r"[a-z_]+", name):
        raise BotError()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def fetch_config(api: GitHub, directory: Path) -> str:
    repository = api.get(api.prefix)
    branch = repository["default_branch"]
    commit = api.get(f"{api.prefix}/commits/{urllib.parse.quote(branch, safe='')}")
    sha = commit["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BotError()
    config = api.get(f"{api.prefix}/contents/.github/.wlreviser-bot.yaml?ref={sha}")
    if config["type"] != "file" or config["encoding"] != "base64":
        raise BotError()
    content = base64.b64decode("".join(config["content"].split()), validate=True)
    private_write(directory / "config.yaml", content)
    return branch


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise BotError()
    return parsed


def recent(value: str, now: datetime) -> bool:
    return now - timedelta(days=30) < timestamp(value) <= now + timedelta(minutes=5)


def report_name(number: int) -> str:
    return f"translations-pr-{number}"


def validate_report(content: bytes, repository: str, number: int, now: datetime) -> None:
    if len(content) > MAX_BYTES:
        raise BotError()
    report = json.loads(content)
    if (
        report["schema_version"] != 2
        or report["repository"] != repository
        or report["pull_request_number"] != number
        or report["pull_request_url"] != f"https://github.com/{repository}/pull/{number}"
        or not recent(report["created_at"], now)
    ):
        raise BotError()


def artifacts(api: GitHub, number: int) -> list[dict[str, Any]]:
    name = report_name(number)
    entries = api.pages(f"{api.prefix}/actions/artifacts?name={name}", "artifacts")
    return [entry for entry in entries if entry["name"] == name]


def delete_reports(api: GitHub, number: int) -> None:
    entries = artifacts(api, number)
    for entry in entries:
        api.mutate("DELETE", f"{api.prefix}/actions/artifacts/{int(entry['id'])}")


def producer_succeeded(api: GitHub, run: dict[str, Any], run_id: int) -> bool:
    if run["status"] == "completed":
        return run["conclusion"] == "success"
    if run["status"] != "in_progress" or run["conclusion"] is not None:
        return False
    jobs = api.pages(f"{api.prefix}/actions/runs/{run_id}/jobs?filter=latest", "jobs")
    return bool(jobs) and all(
        job["status"] == "completed" and job["conclusion"] == "success" for job in jobs
    )


def select_report(api: GitHub, state: dict[str, Any], now: datetime) -> dict[str, Any]:
    candidates = [
        entry
        for entry in artifacts(api, state["number"])
        if not entry["expired"]
        and timestamp(entry["expires_at"]) > now
        and recent(entry["created_at"], now)
    ]
    for entry in sorted(candidates, key=lambda item: timestamp(item["created_at"]), reverse=True):
        run_id = int(entry["workflow_run"]["id"])
        run = api.get(f"{api.prefix}/actions/runs/{run_id}")
        if (
            run["event"] == "issue_comment"
            and run["path"] == state["workflow_path"]
            and run["head_branch"] == state["default_branch"]
            and run["head_repository"]["full_name"] == api.repository
            and run["repository"]["full_name"] == api.repository
            and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
            and producer_succeeded(api, run, run_id)
        ):
            return entry
    raise MissingReport()


def download_archive(api: GitHub, artifact: dict[str, Any]) -> bytes:
    status, _content, headers = api.request(
        "GET", f"{api.prefix}/actions/artifacts/{int(artifact['id'])}/zip"
    )
    if status in {404, 410}:
        raise MissingReport()
    location = {key.lower(): value for key, value in headers.items()}.get("location", "")
    url = urllib.parse.urlsplit(location)
    if status != 302 or url.scheme != "https" or not url.hostname or url.username or url.password:
        raise BotError()
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(urllib.request.Request(location), timeout=60) as response:
        archive = response.read(MAX_BYTES + 1)
    if len(archive) > MAX_BYTES:
        raise BotError()
    digest = artifact.get("digest")
    if digest and digest != f"sha256:{hashlib.sha256(archive).hexdigest()}":
        raise BotError()
    return archive


def extract_report(archive: bytes, filename: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        if len(entries) != 1:
            raise BotError()
        entry = entries[0]
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            entry.filename != filename
            or entry.is_dir()
            or entry.file_size > MAX_BYTES
            or mode not in {0, stat.S_IFREG}
            or entry.flag_bits & 1
        ):
            raise BotError()
        with zipped.open(entry) as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise BotError()
        return content


def preflight(api: GitHub, directory: Path) -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    raw_authors = json.loads(os.environ.get("ALLOWED_PR_AUTHORS", '["weblate"]'))
    if not isinstance(raw_authors, list):
        raise Denied()
    authors = cast(list[Any], raw_authors)
    if not authors or not all(
        isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", author)
        for author in authors
    ):
        raise Denied()
    command = authorize(
        api, event, authors, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_RUN_ATTEMPT"]
    )
    number = int(event["issue"]["number"])
    workflow_ref = os.environ["GITHUB_WORKFLOW_REF"]
    if not workflow_ref.startswith(f"{api.repository}/.github/workflows/"):
        raise Denied()
    directory.mkdir(mode=0o700)
    state: dict[str, Any] = {
        "authorized": True,
        "number": number,
        "comment_id": int(event["comment"]["id"]),
        "verb": command.verb,
        "selectors": command.selectors,
        "workflow_path": workflow_ref.removeprefix(f"{api.repository}/").split("@", 1)[0],
    }
    save_state(directory, state)
    set_output("authorized", "true")
    api.mutate(
        "POST", f"{api.prefix}/issues/comments/{state['comment_id']}/reactions", {"content": "eyes"}
    )
    state["default_branch"] = fetch_config(api, directory)
    save_state(directory, state)
    if command.verb == "apply":
        try:
            now = datetime.now(UTC)
            artifact = select_report(api, state, now)
            content = extract_report(download_archive(api, artifact), f"{report_name(number)}.json")
            validate_report(content, api.repository, number, now)
            private_write(directory / f"{report_name(number)}.json", content)
        except MissingReport:
            state["missing_report"] = True
            save_state(directory, state)
            return
    set_output("proceed", "true")


def child_environment(verb: str) -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({"NO_COLOR": "1", "TERM": "dumb", "PYTHONUTF8": "1", "COLUMNS": "120"})
    names = TOKEN_NAMES if verb == "verify" else ("WEBLATE_TOKEN",)
    for name in names:
        value = os.environ.get(name, "")
        if not value and name != "WL_BOT_AI_TOKEN":
            raise BotError()
        if value:
            environment[name] = value
    return environment


def redact(text: str) -> str:
    tokens = sorted({os.environ.get(name, "") for name in TOKEN_NAMES}, key=len, reverse=True)
    for token in tokens:
        if token:
            text = text.replace(token, "[REDACTED]")
    return text


def install(directory: Path) -> None:
    if not load_state(directory).get("authorized"):
        raise Denied()
    commands = [
        [sys.executable, "-m", "venv", str(directory / "venv")],
        [
            str(directory / "venv/bin/python"),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(Path(__file__).resolve().parents[2]),
        ],
    ]
    for command in commands:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            shell=False,
            cwd=directory,
        )


def execute(api: GitHub, directory: Path) -> None:
    state = load_state(directory)
    if not state.get("authorized") or state.get("executed"):
        raise Denied()
    state["executed"] = True
    save_state(directory, state)
    number = state["number"]
    report_path = directory / f"{report_name(number)}.json"
    command = [str(directory / "venv/bin/wlreviser"), state["verb"], str(directory / "config.yaml")]
    if state["verb"] == "verify":
        command.extend([f"https://github.com/{api.repository}/pull/{number}", str(report_path)])
    else:
        command.extend([str(report_path), *state["selectors"]])
    stdout_path = directory / "stdout.txt"
    stderr_path = directory / "stderr.txt"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(
            command,
            shell=False,
            stdout=stdout,
            stderr=stderr,
            timeout=3000,
            env=child_environment(state["verb"]),
            cwd=directory,
            check=False,
        )
    if result.returncode != 0:
        raise BotError()
    output = redact(stdout_path.read_text(encoding="utf-8"))
    private_write(directory / "output.txt", output.encode())
    if state["verb"] == "verify":
        content = report_path.read_bytes()
        validate_report(content, api.repository, number, datetime.now(UTC))
        text = content.decode("utf-8")
        if redact(text) != text or redact(json.dumps(json.loads(text), ensure_ascii=False)) != (
            json.dumps(json.loads(text), ensure_ascii=False)
        ):
            raise BotError()
        delete_reports(api, number)
        set_output("report_path", str(report_path))
        set_output("artifact_name", report_name(number))
    state["succeeded"] = True
    save_state(directory, state)


def comment_bodies(text: str, comment_id: int, footer: str) -> list[str]:
    chunks = [text[offset : offset + 10000] for offset in range(0, len(text), 10000)] or [""]
    return [
        f"<!-- wlreviser-bot:{comment_id}:{index} -->\n"
        f"<pre>{html.escape(chunk, quote=False)}</pre>\n\n"
        f"{footer}\nPart {index} of {len(chunks)}."
        for index, chunk in enumerate(chunks, start=1)
    ]


def post_once(api: GitHub, number: int, bodies: list[str]) -> None:
    path = f"{api.prefix}/issues/{number}/comments"
    for body in bodies:
        for attempt in range(2):
            existing = api.pages(path)
            if any(
                comment["user"]["login"] == "github-actions[bot]" and comment["body"] == body
                for comment in existing
            ):
                break
            try:
                api.mutate("POST", path, {"body": body})
                break
            except Exception:
                if attempt:
                    raise


def publish(api: GitHub, directory: Path) -> None:
    if not (directory / "state.json").exists():
        return
    state = load_state(directory)
    if not state.get("authorized") or state.get("published"):
        return
    if state.get("missing_report"):
        bodies = [MISSING_REPORT]
    elif os.environ.get("BOT_JOB_STATUS") != "success" or not state.get("succeeded"):
        bodies = [FAILURE]
    else:
        run_id = int(os.environ["GITHUB_RUN_ID"])
        footer = f"[Workflow run](https://github.com/{api.repository}/actions/runs/{run_id})"
        if state["verb"] == "verify":
            artifact_id = int(os.environ["BOT_ARTIFACT_ID"])
            remaining = artifacts(api, state["number"])
            if len(remaining) != 1 or remaining[0]["id"] != artifact_id:
                raise BotError()
            footer += (
                f" | [JSON report](https://github.com/{api.repository}/actions/runs/"
                f"{run_id}/artifacts/{artifact_id})"
            )
        bodies = comment_bodies(
            (directory / "output.txt").read_text(encoding="utf-8"), state["comment_id"], footer
        )
    if bodies in ([FAILURE], [MISSING_REPORT]):
        bodies = [f"<!-- wlreviser-bot:{state['comment_id']}:status -->\n{bodies[0]}"]
    post_once(api, state["number"], bodies)
    state["published"] = True
    save_state(directory, state)


def work_directory() -> Path:
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise BotError()
    return Path(os.environ["RUNNER_TEMP"]) / f"wlreviser-bot-{run_id}-{attempt}"


def main() -> int:
    os.umask(0o077)
    try:
        phase = sys.argv[1]
        directory = work_directory()
        if phase == "cleanup":
            if directory.exists():
                shutil.rmtree(directory)
            return 0
        if phase == "install":
            install(directory)
            return 0
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        if phase == "preflight":
            preflight(api, directory)
        elif phase == "execute":
            execute(api, directory)
        elif phase == "publish":
            try:
                publish(api, directory)
            except Exception:
                os.environ["BOT_JOB_STATUS"] = "failure"
                publish(api, directory)
                raise
        else:
            raise BotError()
        return 0
    except Denied:
        print("Translation request was not accepted.")
        return 0
    except Exception:
        print(FAILURE)
        return 1


if __name__ == "__main__":
    sys.exit(main())
