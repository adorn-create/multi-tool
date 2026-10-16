import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlparse

import paramiko
from flask import Flask, current_app, jsonify, redirect, render_template, request, session, url_for
from ollama import Client, RequestError, ResponseError
from waitress import serve

OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
CF_ACCESS_CLIENT_ID = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
CF_ACCESS_CLIENT_SECRET = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
OLLAMA_HOST = os.getenv(
    "OLLAMA_HOST",
    "https://ollama.com" if OLLAMA_API_KEY else "http://localhost:11434",
).strip().rstrip("/")
OLLAMA_HOSTNAME = urlparse(OLLAMA_HOST).hostname
IS_OFFICIAL_OLLAMA_CLOUD = OLLAMA_HOSTNAME == "ollama.com"
MODEL_NAMES = ("hermes3", "qwen2.5:14b")
MODEL_LABEL = " + ".join(MODEL_NAMES)
ALLOW_VM_COMMANDS = os.getenv("ALLOW_VM_COMMANDS", "").lower() == "true"
ALLOW_TOOL_INSTALLS = os.getenv("ALLOW_TOOL_INSTALLS", "").lower() == "true"
MAX_COMMAND_LENGTH = 2000
MAX_OUTPUT_LENGTH = 200_000
MAX_PROMPT_LENGTH = 2000
MAX_VM_COMMAND_TIMEOUT_SECONDS = 600
LOGIN_ATTEMPT_WINDOW = 15 * 60
MAX_LOGIN_ATTEMPTS = 5
DEFAULT_SESSION_IDLE_TIMEOUT_MINUTES = 15
KALI_TOOL_CATALOG = {
    "nmap": "Network discovery and service checks for systems you are authorized to test.",
    "tcpdump": "Capture and inspect network traffic on your VM.",
    "wireshark": "Analyze packet captures and network protocols.",
    "dnsutils": "DNS lookup utilities such as dig and nslookup.",
    "whois": "Look up domain registration information.",
    "net-tools": "Legacy network utilities such as ifconfig and netstat.",
    "git": "Clone and manage source code repositories.",
    "python3-venv": "Create isolated Python virtual environments.",
}

SYSTEM_PROMPT = (
    "You are Nigash, an assistant for the user's authorized Kali Linux VM. "
    "Return exactly one JSON object, without markdown, in one of these forms: "
    '{"action":"chat","message":"your reply"} or '
    '{"action":"command","command":"one shell command","message":"brief explanation"}. '
    "Use action=command only when the user's request requires running a terminal command. "
    "Use action=chat for ordinary questions. Never claim a command ran when it did not. "
    "Keep chat replies concise, usually one to three sentences. Keep command explanations brief. "
    "Only provide commands for systems the user owns or is authorized to administer."
)
SYNTHESIS_SYSTEM_PROMPT = (
    "You are Nigash. Combine two independent assistant responses into one concise answer for the user. "
    "Return exactly one JSON object in this form: "
    '{"action":"chat","message":"your unified reply"}. '
    "Never provide or change a terminal command. If the responses disagree about a command, "
    "explain the disagreement and say no command will be run."
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


def generate_model_response(
    client: Client,
    model: str,
    system: str,
    prompt: str,
) -> tuple[str, str, str]:
    response = client.generate(
        model=model,
        system=system,
        prompt=prompt,
        stream=False,
        keep_alive="0",
        options={"num_ctx": 4096, "num_predict": 300, "temperature": 0.2},
    )
    raw_response = response.get("response", "").strip()
    if not raw_response:
        raise ValueError(f'Ollama model "{model}" returned an empty response.')
    return parse_ai_response(raw_response)


def create_ollama_client() -> Client:
    headers: dict[str, str] = {}
    if IS_OFFICIAL_OLLAMA_CLOUD:
        if OLLAMA_API_KEY:
            headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    elif CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_ACCESS_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET
    return Client(host=OLLAMA_HOST, headers=headers)


def run_command_in_vm(
    host: str,
    username: str,
    password: str,
    command: str,
    key_filename: str = "",
    key_passphrase: str = "",
    timeout_seconds: int = 60,
) -> str:
    """Run a command only on a VM whose SSH host key is already trusted."""
    if not host.strip() or not username.strip():
        raise ValueError("Set KALI_VM_IP and KALI_VM_USER before running VM commands.")
    if not 1 <= timeout_seconds <= MAX_VM_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(
            f"VM command timeout must be between 1 and {MAX_VM_COMMAND_TIMEOUT_SECONDS} seconds."
        )

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
        _, stdout, _ = ssh.exec_command(command, timeout=timeout_seconds)
        channel = stdout.channel
        output = bytearray()
        error = bytearray()
        output_truncated = False
        error_truncated = False
        deadline = time.monotonic() + timeout_seconds

        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if time.monotonic() >= deadline:
                channel.close()
                raise TimeoutError(
                    f"The VM command exceeded the {timeout_seconds}-second limit."
                )
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
            "Add them in your hosting provider's Variables and secrets settings, then restart the container."
        )
    if bool(CF_ACCESS_CLIENT_ID) != bool(CF_ACCESS_CLIENT_SECRET):
        raise RuntimeError(
            "Set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET, or remove both, "
            "to configure Cloudflare Access service authentication."
        )
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
        error = "You were signed out due to inactivity. Please sign in again." if request.args.get("expired") else None
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
            return render_template("login.html", error="Too many attempts. Please wait before trying again."), 429

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
        csrf = csrf_token()
        tool_confirmations = {
            package: hmac.new(
                app.config["SECRET_KEY"].encode("utf-8"),
                f"{csrf}:install:{package}".encode("utf-8"),
                sha256,
            ).hexdigest()
            for package in KALI_TOOL_CATALOG
        } if ALLOW_TOOL_INSTALLS else {}
        return render_template(
            "index.html",
            model_name=MODEL_LABEL,
            allow_vm_commands=ALLOW_VM_COMMANDS,
            allow_tool_installs=ALLOW_TOOL_INSTALLS,
            tool_catalog=KALI_TOOL_CATALOG if ALLOW_TOOL_INSTALLS else {},
            tool_confirmations=tool_confirmations,
            idle_timeout_minutes=app.config["SESSION_IDLE_TIMEOUT_MINUTES"],
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
            client = create_ollama_client()
            model_responses = [
                (
                    model,
                    generate_model_response(client, model, SYSTEM_PROMPT, prompt),
                )
                for model in MODEL_NAMES
            ]
            synthesis_prompt = json.dumps(
                {
                    "user_request": prompt,
                    "model_responses": [
                        {
                            "model": model,
                            "action": response[0],
                            "message": response[1],
                            "command": response[2] if response[0] == "command" else "",
                        }
                        for model, response in model_responses
                    ],
                }
            )
            synthesis_action, message, _ = generate_model_response(
                client,
                MODEL_NAMES[0],
                SYNTHESIS_SYSTEM_PROMPT,
                synthesis_prompt,
            )
            if synthesis_action != "chat":
                raise ValueError("The synthesis response must be a chat message.")

            proposed_commands = [
                response[2] for _, response in model_responses if response[0] == "command"
            ]
            command = ""
            action = "chat"
            if (
                len(proposed_commands) == len(MODEL_NAMES)
                and all(candidate == proposed_commands[0] for candidate in proposed_commands)
            ):
                action = "command"
                command = proposed_commands[0]
            elif proposed_commands:
                message += "\n\nThe models disagreed on a command, so no command will be run."
        except ResponseError as exc:
            current_app.logger.exception("Ollama request failed.")
            if exc.status_code in (401, 403):
                host = urlparse(OLLAMA_HOST).hostname or "configured Ollama host"
                detail = exc.error.strip()[:300]
                if host == "ollama.com":
                    message = "Ollama Cloud rejected authentication. Check the OLLAMA_API_KEY secret and account access."
                else:
                    message = (
                        f"The configured Ollama endpoint ({host}) rejected authentication. "
                        "If this tunnel is protected by Cloudflare Access, add a service token using "
                        "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET in the hosting secrets. "
                        "Otherwise check the Ollama server or proxy's authentication settings."
                    )
                if detail:
                    message += f" Server response: {detail}"
            elif exc.status_code == 404:
                missing_models = ", ".join(MODEL_NAMES)
                message = (
                    f"Ollama could not find one of the required models ({missing_models}). "
                    "Pull both models on the Ollama server with `ollama pull hermes3` and "
                    "`ollama pull qwen2.5:14b`."
                )
            else:
                detail = exc.error.strip()[:300]
                message = f"Ollama returned HTTP {exc.status_code}."
                if detail:
                    message += f" {detail}"
            return jsonify(error=message), 502
        except (ValueError, json.JSONDecodeError):
            current_app.logger.exception("An Ollama model returned an invalid response.")
            return jsonify(
                error=(
                    "An Ollama model returned a response in an unexpected format. "
                    "Check that every configured model supports the assistant's required JSON response format."
                )
            ), 502
        except RequestError:
            current_app.logger.exception("Could not connect to Ollama.")
            return jsonify(
                error=(
                    "Could not connect to Ollama. Set OLLAMA_HOST to the reachable Ollama server URL "
                    "(for example, your Railway private service URL or Cloudflare tunnel URL), "
                    "and confirm that server is running."
                )
            ), 502
        except Exception:
            current_app.logger.exception("Unexpected Ollama request failure.")
            return jsonify(
                error="The AI service failed unexpectedly. Check the application logs for the server-side details."
            ), 502

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

    @app.post("/api/tools/install")
    @login_required
    def install_tool() -> Any:
        if not ALLOW_TOOL_INSTALLS:
            return jsonify(error="VM tool installation is disabled on this website."), 403
        if not csrf_is_valid():
            return jsonify(error="Your session expired. Refresh the page and try again."), 400

        payload = request.get_json(silent=True)
        package = payload.get("package") if isinstance(payload, dict) else None
        confirmation = payload.get("confirmation") if isinstance(payload, dict) else None
        if not isinstance(package, str) or package not in KALI_TOOL_CATALOG:
            return jsonify(error="Choose a tool from the supported Kali package catalog."), 400
        if not isinstance(confirmation, str):
            return jsonify(error="Confirm this tool installation from the website first."), 400

        expected_confirmation = hmac.new(
            app.config["SECRET_KEY"].encode("utf-8"),
            f'{session.get("_csrf_token", "")}:install:{package}'.encode("utf-8"),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(confirmation, expected_confirmation):
            return jsonify(error="This installation was not confirmed for your current session."), 400

        command = (
            "sudo -n apt-get update && "
            f"sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y "
            f"--no-install-recommends -- {package}"
        )
        try:
            result = run_command_in_vm(
                os.getenv("KALI_VM_IP", ""),
                os.getenv("KALI_VM_USER", ""),
                os.getenv("KALI_VM_PASS", ""),
                command,
                os.getenv("KALI_SSH_KEY", ""),
                os.getenv("KALI_SSH_KEY_PASSPHRASE", ""),
                timeout_seconds=MAX_VM_COMMAND_TIMEOUT_SECONDS,
            )
        except Exception:
            current_app.logger.exception("Kali package installation failed.")
            return jsonify(
                error=(
                    "The tool installation failed. Check the VM SSH configuration, package "
                    "repositories, network access, and passwordless sudo permissions."
                )
            ), 502
        if not result.startswith("Exit code: 0\n"):
            current_app.logger.warning(
                "APT installation of allowlisted package %s failed: %s",
                package,
                result[:1000],
            )
            return jsonify(
                error=(
                    f"APT could not install {package}. Check the VM's Kali APT repositories, "
                    "permissions, and network access."
                )
            ), 502
        return jsonify(package=package, result=result)

    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port_value = os.getenv("PORT", "").strip() or os.getenv("WEB_PORT", "").strip() or "8080"
    port = int(port_value)
    serve(create_app(), host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
