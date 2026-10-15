import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlparse

import paramiko
from flask import Flask, current_app, jsonify, redirect, render_template, request, session, url_for
from ollama import Client
from waitress import serve

OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_HOST = os.getenv(
    "OLLAMA_HOST",
    "https://ollama.com" if OLLAMA_API_KEY else "http://localhost:11434",
)
MODEL_NAME = os.getenv(
    "OLLAMA_MODEL",
    "gpt-oss:120b" if OLLAMA_API_KEY else "hf.co/bartowski/Qwen2.5-14B-Instruct-GGUF:Q4_K_M",
)
ALLOW_VM_COMMANDS = os.getenv("ALLOW_VM_COMMANDS", "").lower() == "true"
MAX_COMMAND_LENGTH = 2000
MAX_OUTPUT_LENGTH = 200_000
MAX_PROMPT_LENGTH = 2000
LOGIN_ATTEMPT_WINDOW = 15 * 60
MAX_LOGIN_ATTEMPTS = 5

SYSTEM_PROMPT = (
    "You are an assistant for the user's authorized Kali Linux VM. "
    "Return exactly one JSON object, without markdown, in one of these forms: "
    '{"action":"chat","message":"your reply"} or '
    '{"action":"command","command":"one shell command","message":"brief explanation"}. '
    "Use action=command only when the user's request requires running a terminal command. "
    "Use action=chat for ordinary questions. Never claim a command ran when it did not. "
    "Keep chat replies concise, usually one to three sentences. Keep command explanations brief. "
    "Only provide commands for systems the user owns or is authorized to administer."
)


def parse_ai_response(response_text: str) -> tuple[str, str, str]:
    """Return the action, its message, and an optional terminal command."""
    response = json.loads(response_text)
    if not isinstance(response, dict):
        raise ValueError("The AI response must be a JSON object.")

    action = response.get("action")
    message = response.get("message", "")
    if not isinstance(message, str):
        raise ValueError("The AI response message must be text.")

    if action == "chat":
        return action, message, ""
    if action != "command":
        raise ValueError("The AI response action must be 'chat' or 'command'.")

    command = response.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("The AI did not provide a terminal command.")
    command = command.strip()
    if len(command) > MAX_COMMAND_LENGTH or any(char in command for char in ("\n", "\r", "\x00")):
        raise ValueError("The terminal command is too long or contains invalid characters.")
    return action, message, command


def create_ollama_client() -> Client:
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    return Client(host=OLLAMA_HOST, headers=headers)


def run_command_in_vm(
    host: str,
    username: str,
    password: str,
    command: str,
    key_filename: str = "",
    key_passphrase: str = "",
) -> str:
    """Run a command only on a VM whose SSH host key is already trusted."""
    if not host.strip() or not username.strip():
        raise ValueError("Set KALI_VM_IP and KALI_VM_USER before running VM commands.")

    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    known_hosts = os.getenv("KALI_SSH_KNOWN_HOSTS", "").strip()
    if known_hosts:
        ssh.load_host_keys(known_hosts)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        ssh.connect(
            hostname=host.strip(),
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
        _, stdout, _ = ssh.exec_command(command, timeout=60)
        channel = stdout.channel
        output = bytearray()
        error = bytearray()
        output_truncated = False
        error_truncated = False
        deadline = time.monotonic() + 60

        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if time.monotonic() >= deadline:
                channel.close()
                raise TimeoutError("The VM command exceeded the 60-second limit.")
            received_data = False
            if channel.recv_ready():
                chunk = channel.recv(32_768)
                available = MAX_OUTPUT_LENGTH - len(output)
                output.extend(chunk[:available])
                output_truncated |= len(chunk) > available
                received_data = True
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(32_768)
                available = MAX_OUTPUT_LENGTH - len(error)
                error.extend(chunk[:available])
                error_truncated |= len(chunk) > available
                received_data = True
            if not received_data:
                time.sleep(0.05)

        exit_code = channel.recv_exit_status()
        output_text = output.decode("utf-8", errors="replace").strip()
        error_text = error.decode("utf-8", errors="replace").strip()
        details = "\n".join(part for part in (output_text, error_text) if part)
        if output_truncated or error_truncated:
            details += "\n[Output truncated]"
        if not details:
            details = "(no output)"
        return f"Exit code: {exit_code}\n{details}"
    except paramiko.AuthenticationException as exc:
        raise RuntimeError(
            "SSH rejected the login. Check the VM username/password or configured private key."
        ) from exc
    except paramiko.SSHException as exc:
        if "no authentication methods available" in str(exc).lower():
            raise RuntimeError(
                "The SSH server offered no usable login method. Configure a password or private key."
            ) from exc
        raise
    finally:
        ssh.close()


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", ""),
        SITE_PASSWORD=os.getenv("SITE_PASSWORD", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower() == "true",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] or not app.config["SITE_PASSWORD"]:
        raise RuntimeError("Set FLASK_SECRET_KEY and SITE_PASSWORD before starting the website.")
    if urlparse(OLLAMA_HOST).hostname == "ollama.com" and not OLLAMA_API_KEY:
        raise RuntimeError("Set OLLAMA_API_KEY when connecting directly to Ollama Cloud.")

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
        return render_template("login.html", error=None)

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
            return render_template("login.html", error="Too many attempts. Please wait before trying again."), 429

        password = request.form.get("password", "")
        if not hmac.compare_digest(password, app.config["SITE_PASSWORD"]):
            attempts.append(now)
            return render_template("login.html", error="That password was not accepted."), 401

        login_attempts.pop(client_address, None)
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        if not csrf_is_valid():
            return "Your sign-out form expired. Please try again.", 400
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index() -> Any:
        return render_template(
            "index.html",
            model_name=MODEL_NAME,
            allow_vm_commands=ALLOW_VM_COMMANDS,
        )

    @app.post("/api/chat")
    @login_required
    def chat() -> Any:
        if not csrf_is_valid():
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        payload = request.get_json(silent=True)
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify(error="Enter a message first."), 400
        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_LENGTH:
            return jsonify(error=f"Messages must be {MAX_PROMPT_LENGTH} characters or fewer."), 400

        try:
            response = create_ollama_client().generate(
                model=MODEL_NAME,
                system=SYSTEM_PROMPT,
                prompt=prompt,
                stream=False,
                keep_alive="30m",
                options={"num_ctx": 4096, "num_predict": 160, "temperature": 0.2},
            )
        except Exception:
            current_app.logger.exception("Ollama request failed.")
            return jsonify(error="The AI service could not complete your request. Check the server configuration."), 502
        raw_response = response.get("response", "").strip()
        if not raw_response:
            return jsonify(error="The AI returned an empty response."), 502
        try:
            action, message, command = parse_ai_response(raw_response)
        except (ValueError, json.JSONDecodeError) as exc:
            current_app.logger.warning("Could not parse the AI response: %s", exc)
            return jsonify(error="The AI returned a response that could not be processed."), 502

        result: dict[str, str] = {"action": action, "message": message or "(no response text)"}
        if action == "command":
            if not ALLOW_VM_COMMANDS:
                result["action"] = "chat"
                result["message"] += "\n\nVM command execution is disabled on this website."
                return jsonify(result)
            csrf = session.get("_csrf_token", "")
            signature = hmac.new(
                app.config["SECRET_KEY"].encode("utf-8"),
                f"{csrf}:{command}".encode("utf-8"),
                sha256,
            ).hexdigest()
            result["command"] = command
            result["confirmation"] = signature
        return jsonify(result)

    @app.post("/api/execute")
    @login_required
    def execute_command() -> Any:
        if not ALLOW_VM_COMMANDS:
            return jsonify(error="VM command execution is disabled on this website."), 403
        if not csrf_is_valid():
            return jsonify(error="Your session expired. Refresh the page and try again."), 400
        payload = request.get_json(silent=True)
        command = payload.get("command") if isinstance(payload, dict) else None
        confirmation = payload.get("confirmation") if isinstance(payload, dict) else None
        if not isinstance(command, str) or not isinstance(confirmation, str):
            return jsonify(error="A command confirmation is required."), 400
        command = command.strip()
        if (
            not command
            or len(command) > MAX_COMMAND_LENGTH
            or any(character in command for character in ("\n", "\r", "\x00"))
        ):
            return jsonify(error="The command is empty, too long, or contains invalid characters."), 400
        expected_confirmation = hmac.new(
            app.config["SECRET_KEY"].encode("utf-8"),
            f'{session.get("_csrf_token", "")}:{command}'.encode("utf-8"),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(confirmation, expected_confirmation):
            return jsonify(error="This command was not generated for your current session. Ask the assistant again."), 400

        try:
            result = run_command_in_vm(
                os.getenv("KALI_VM_IP", ""),
                os.getenv("KALI_VM_USER", ""),
                os.getenv("KALI_VM_PASS", ""),
                command,
                os.getenv("KALI_SSH_KEY", ""),
                os.getenv("KALI_SSH_KEY_PASSPHRASE", ""),
            )
        except Exception:
            current_app.logger.exception("VM command failed.")
            return jsonify(error="The VM command failed. Check server logs and VM configuration."), 502
        return jsonify(result=result)

    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", "8000")))
    serve(create_app(), host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
