import hmac
import json
import logging
import os
import re
import secrets
import shlex
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

import paramiko
from flask import Flask, current_app, jsonify, redirect, render_template, request, session, url_for
from waitress import serve

MAX_PROMPT_LENGTH = 2000
LOGIN_ATTEMPT_WINDOW = 15 * 60
MAX_LOGIN_ATTEMPTS = 5
DEFAULT_SESSION_IDLE_TIMEOUT_MINUTES = 15
HERMES_CLI = os.getenv("HERMES_CLI", "hermes").strip()
HERMES_KANBAN_BOARD = os.getenv("HERMES_KANBAN_BOARD", "default").strip()
HERMES_TEAM_LEAD_PROFILE = os.getenv("HERMES_TEAM_LEAD_PROFILE", "security-lead").strip()
HERMES_COMMAND_TIMEOUT_SECONDS = 30
MAX_HERMES_OUTPUT_BYTES = 1_000_000
HERMES_KANBAN_STATUSES = frozenset(
    {"triage", "todo", "ready", "running", "blocked", "review", "done", "archived"}
)
HERMES_TEAM_AGENTS = (
    {
        "profile": "security-lead",
        "title": "Security lead",
        "role": "Scopes work and coordinates the team.",
    },
    {
        "profile": "recon",
        "title": "Recon analyst",
        "role": "Maps authorized targets and attack surface.",
    },
    {
        "profile": "web-auditor",
        "title": "Web auditor",
        "role": "Reviews web applications and configurations.",
    },
    {
        "profile": "vulnerability-analyst",
        "title": "Vulnerability analyst",
        "role": "Validates evidence and prioritizes findings.",
    },
    {
        "profile": "report-writer",
        "title": "Report writer",
        "role": "Turns verified findings into remediation guidance.",
    },
)


class HermesKanbanError(RuntimeError):
    """An explicit error returned by the local Hermes Kanban CLI."""


def run_hermes_kanban(
    host: str,
    port: int,
    username: str,
    password: str,
    key_filename: str,
    key_passphrase: str,
    known_hosts: str,
    cli_path: str,
    board: str,
    arguments: list[str],
    input_text: str | None = None,
) -> Any:
    """Run a fixed Hermes Kanban command over verified SSH without interpolating task data."""
    if not host.strip() or not username.strip():
        raise HermesKanbanError("Set KALI_VM_IP and KALI_VM_USER to connect to your Kali VM.")
    if not password and not key_filename:
        raise HermesKanbanError("Set KALI_VM_PASS or KALI_SSH_KEY for Kali VM authentication.")

    command = " ".join(
        shlex.quote(part) for part in (cli_path, "kanban", "--board", board, *arguments)
    )
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        if known_hosts:
            ssh.load_host_keys(known_hosts)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            hostname=host.strip(),
            port=port,
            username=username.strip(),
            key_filename=key_filename.strip() or None,
            passphrase=key_passphrase or None,
            password=password or None,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        stdin, stdout, _ = ssh.exec_command(
            command,
            timeout=HERMES_COMMAND_TIMEOUT_SECONDS,
        )
        if input_text is not None:
            stdin.channel.sendall(input_text.encode("utf-8"))
        stdin.channel.shutdown_write()

        channel = stdout.channel
        output = bytearray()
        error = bytearray()
        deadline = time.monotonic() + HERMES_COMMAND_TIMEOUT_SECONDS
        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if time.monotonic() >= deadline:
                channel.close()
                raise HermesKanbanError("Hermes Kanban did not respond before the 30-second limit.")
            received_data = False
            if channel.recv_ready():
                output.extend(channel.recv(32_768))
                received_data = True
            if channel.recv_stderr_ready():
                error.extend(channel.recv_stderr(32_768))
                received_data = True
            if len(output) + len(error) > MAX_HERMES_OUTPUT_BYTES:
                channel.close()
                raise HermesKanbanError("Hermes Kanban returned more than 1 MB of output.")
            if not received_data:
                time.sleep(0.05)

        exit_code = channel.recv_exit_status()
        if exit_code:
            detail = error.decode("utf-8", errors="replace").strip()
            if detail:
                raise HermesKanbanError(f"Hermes Kanban failed: {detail[-500:]}")
            raise HermesKanbanError("Hermes Kanban failed without returning an error message.")

        try:
            return json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HermesKanbanError("Hermes Kanban returned invalid JSON.") from exc
    except paramiko.AuthenticationException as exc:
        raise HermesKanbanError(
            "SSH authentication to the Kali VM failed. Check its username and password/key."
        ) from exc
    except (OSError, paramiko.SSHException, TimeoutError) as exc:
        raise HermesKanbanError(f"Could not connect to the Kali VM over verified SSH: {exc}") from exc
    finally:
        ssh.close()


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    try:
        idle_timeout_minutes = int(
            os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", str(DEFAULT_SESSION_IDLE_TIMEOUT_MINUTES))
        )
    except ValueError as exc:
        raise RuntimeError("SESSION_IDLE_TIMEOUT_MINUTES must be a positive whole number.") from exc
    if idle_timeout_minutes < 1:
        raise RuntimeError("SESSION_IDLE_TIMEOUT_MINUTES must be at least 1.")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", ""),
        SITE_PASSWORD=os.getenv("SITE_PASSWORD", ""),
        KALI_VM_IP=os.getenv("KALI_VM_IP", "").strip(),
        KALI_SSH_PORT=os.getenv("KALI_SSH_PORT", "22").strip(),
        KALI_VM_USER=os.getenv("KALI_VM_USER", "").strip(),
        KALI_VM_PASS=os.getenv("KALI_VM_PASS", ""),
        KALI_SSH_KEY=os.getenv("KALI_SSH_KEY", "").strip(),
        KALI_SSH_KEY_PASSPHRASE=os.getenv("KALI_SSH_KEY_PASSPHRASE", ""),
        KALI_SSH_KNOWN_HOSTS=os.getenv("KALI_SSH_KNOWN_HOSTS", "").strip(),
        HERMES_CLI=HERMES_CLI,
        HERMES_KANBAN_BOARD=HERMES_KANBAN_BOARD,
        HERMES_TEAM_LEAD_PROFILE=HERMES_TEAM_LEAD_PROFILE,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower() == "true",
        SESSION_IDLE_TIMEOUT_MINUTES=idle_timeout_minutes,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=idle_timeout_minutes),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=app.config["SESSION_IDLE_TIMEOUT_MINUTES"]
    )

    if not app.config["HERMES_CLI"].strip():
        raise RuntimeError("HERMES_CLI must point to the Hermes Agent executable in Kali.")
    try:
        app.config["KALI_SSH_PORT"] = int(app.config["KALI_SSH_PORT"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("KALI_SSH_PORT must be a valid TCP port.") from exc
    if not 1 <= app.config["KALI_SSH_PORT"] <= 65535:
        raise RuntimeError("KALI_SSH_PORT must be between 1 and 65535.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", app.config["HERMES_KANBAN_BOARD"]):
        raise RuntimeError("HERMES_KANBAN_BOARD must be a lowercase Kanban board slug.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", app.config["HERMES_TEAM_LEAD_PROFILE"]):
        raise RuntimeError("HERMES_TEAM_LEAD_PROFILE must be a valid Hermes profile name.")

    missing_settings = [
        name
        for name, value in (
            ("FLASK_SECRET_KEY", app.config["SECRET_KEY"]),
            ("SITE_PASSWORD", app.config["SITE_PASSWORD"]),
        )
        if not value.strip()
    ]
    if missing_settings:
        missing = ", ".join(missing_settings)
        raise RuntimeError(
            f"Missing required environment variable(s): {missing}. "
            "Set them in the local environment before starting the website."
        )

    login_attempts: dict[str, deque[float]] = defaultdict(deque)

    def csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    def csrf_is_valid() -> bool:
        supplied = request.headers.get("X-CSRF-Token") if request.is_json else request.form.get("csrf_token")
        expected = session.get("_csrf_token", "")
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    def run_team_kanban(arguments: list[str], input_text: str | None = None) -> Any:
        return run_hermes_kanban(
            app.config["KALI_VM_IP"],
            app.config["KALI_SSH_PORT"],
            app.config["KALI_VM_USER"],
            app.config["KALI_VM_PASS"],
            app.config["KALI_SSH_KEY"],
            app.config["KALI_SSH_KEY_PASSPHRASE"],
            app.config["KALI_SSH_KNOWN_HOSTS"],
            app.config["HERMES_CLI"],
            app.config["HERMES_KANBAN_BOARD"],
            arguments,
            input_text,
        )

    @app.before_request
    def enforce_idle_timeout() -> Any:
        if not session.get("authenticated"):
            return None

        now = time.time()
        last_activity = session.get("_last_activity", now)
        if not isinstance(last_activity, (int, float)) or now - last_activity >= (
            app.config["SESSION_IDLE_TIMEOUT_MINUTES"] * 60
        ):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Your session expired due to inactivity. Please sign in again."), 401
            return redirect(url_for("login", expired=1))

        session["_last_activity"] = now
        return None

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify(error="Please sign in again."), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    @app.context_processor
    def inject_csrf_token() -> dict[str, Callable[[], str]]:
        return {"csrf_token": csrf_token}

    @app.after_request
    def add_security_headers(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/login")
    def login() -> Any:
        if session.get("authenticated"):
            return redirect(url_for("index"))
        error = (
            "You were signed out due to inactivity. Please sign in again."
            if request.args.get("expired")
            else None
        )
        return render_template("login.html", error=error)

    @app.post("/login")
    def login_submit() -> Any:
        if not csrf_is_valid():
            return render_template("login.html", error="Your sign-in form expired. Please try again."), 400

        client_address = request.remote_addr or "unknown"
        now = time.monotonic()
        attempts = login_attempts[client_address]
        while attempts and now - attempts[0] > LOGIN_ATTEMPT_WINDOW:
            attempts.popleft()
        if len(attempts) >= MAX_LOGIN_ATTEMPTS:
            return render_template(
                "login.html", error="Too many attempts. Please wait before trying again."
            ), 429

        password = request.form.get("password", "")
        if not hmac.compare_digest(password, app.config["SITE_PASSWORD"]):
            attempts.append(now)
            return render_template("login.html", error="That password was not accepted."), 401

        login_attempts.pop(client_address, None)
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["_last_activity"] = time.time()
        csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        if not csrf_is_valid():
            return "Your sign-out form expired. Please try again.", 400
        session.clear()
        return redirect(url_for("login"))

    @app.post("/api/activity")
    @login_required
    def activity() -> Any:
        if not csrf_is_valid():
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        session["_last_activity"] = time.time()
        return jsonify(ok=True)

    @app.get("/")
    @login_required
    def index() -> Any:
        team_agents = [
            {
                **agent,
                "profile": (
                    app.config["HERMES_TEAM_LEAD_PROFILE"]
                    if agent["profile"] == "security-lead"
                    else agent["profile"]
                ),
            }
            for agent in HERMES_TEAM_AGENTS
        ]
        return render_template(
            "index.html",
            idle_timeout_minutes=app.config["SESSION_IDLE_TIMEOUT_MINUTES"],
            team_agents=team_agents,
            kanban_board=app.config["HERMES_KANBAN_BOARD"],
        )

    @app.get("/api/team")
    @login_required
    def team_board() -> Any:
        try:
            tasks = run_team_kanban(["list", "--json"])
        except HermesKanbanError as exc:
            current_app.logger.warning("Could not load the Hermes Kanban board: %s", exc)
            return jsonify(error=str(exc)), 502

        if not isinstance(tasks, list) or any(
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not isinstance(task.get("title"), str)
            or task.get("status") not in HERMES_KANBAN_STATUSES
            for task in tasks
        ):
            current_app.logger.error("Hermes Kanban list returned an unexpected JSON shape.")
            return jsonify(error="Hermes Kanban returned an unexpected task list."), 502
        return jsonify(tasks=tasks, agents=HERMES_TEAM_AGENTS, board=app.config["HERMES_KANBAN_BOARD"])

    @app.post("/api/team/tasks")
    @login_required
    def create_team_task() -> Any:
        if not csrf_is_valid():
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        payload = request.get_json(silent=True)
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        authorized = payload.get("authorized") if isinstance(payload, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify(error="Describe the assessment task first."), 400
        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_LENGTH:
            return jsonify(error=f"Tasks must be {MAX_PROMPT_LENGTH} characters or fewer."), 400
        if authorized is not True:
            return jsonify(error="Confirm that you are authorized to assess the target."), 400

        title = prompt.splitlines()[0].strip()[:100]
        title = f"Authorized assessment: {title}"
        body = (
            "The website user confirms this task is within an authorized assessment scope. "
            "Do not access systems outside the scope stated below. If the scope is unclear, "
            "request clarification before proceeding.\n\n"
            f"{prompt}"
        )
        try:
            task = run_team_kanban(
                [
                    "create",
                    title,
                    "--body-file",
                    "-",
                    "--assignee",
                    app.config["HERMES_TEAM_LEAD_PROFILE"],
                    "--triage",
                    "--json",
                ],
                input_text=body,
            )
        except HermesKanbanError as exc:
            current_app.logger.warning("Could not create a Hermes Kanban task: %s", exc)
            return jsonify(error=str(exc)), 502

        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            current_app.logger.error("Hermes Kanban create returned an unexpected JSON shape.")
            return jsonify(error="Hermes Kanban returned an unexpected task response."), 502
        return jsonify(task=task), 201

    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port_value = os.getenv("PORT", "").strip() or os.getenv("WEB_PORT", "").strip() or "8080"
    port = int(port_value)
    serve(create_app(), host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
