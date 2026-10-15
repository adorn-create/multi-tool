---
title: Kali AI Assistant
sdk: docker
app_port: 8080
---

# Kali AI Assistant website

This project provides a password-protected website for the Kali VM AI assistant. It can call Ollama Cloud directly with a server-side API key, so the web host does not need to run a local Ollama model.

## Start locally

Install dependencies, then set the required secrets and start the server:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SITE_PASSWORD = Read-Host "Enter the shared website password"
$env:FLASK_SECRET_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))")
$env:OLLAMA_API_KEY = Read-Host "Enter your Ollama Cloud API key"
$env:OLLAMA_HOST = "https://ollama.com"
$env:OLLAMA_MODEL = "gpt-oss:120b"
$env:KALI_VM_IP = "your-vm-address"
$env:KALI_VM_USER = "your-vm-username"
.\.venv\Scripts\python.exe .\ai.py
```

Open `http://127.0.0.1:8080`. Create the cloud API key in [Ollama API keys](https://ollama.com/settings/keys). `OLLAMA_API_KEY` selects direct Ollama Cloud by default; the app sends it only from the server to `https://ollama.com`. Use a model name supported by the cloud API, such as `gpt-oss:120b`, and set `OLLAMA_MODEL` to choose another. For a separately hosted Ollama server (including Railway), set `OLLAMA_HOST` to its reachable base URL and `OLLAMA_MODEL` to a model installed on that server, for example `qwen2.5:14b`. For local Ollama, omit `OLLAMA_API_KEY` and set `OLLAMA_HOST=http://localhost:11434`.

## Public hosting

The Dockerfile works with Render and Hugging Face Spaces Docker. Configure these deployment secrets/environment variables in the host dashboard, not in source code:

| Variable | Purpose |
| --- | --- |
| `SITE_PASSWORD` | Shared password visitors enter to access the website |
| `FLASK_SECRET_KEY` | Unique random secret used to sign sessions; keep it stable across restarts |
| `OLLAMA_API_KEY` | Ollama Cloud API key; never add it to browser code or commit it |
| `OLLAMA_HOST` | `https://ollama.com` for direct cloud API (default when the API key is set) |
| `OLLAMA_MODEL` | Cloud model identifier, e.g. `gpt-oss:120b` |
| `WEB_HOST` | Set to `0.0.0.0` in the container platform |
| `PORT` / `WEB_PORT` | Platform port (`PORT` is detected automatically; otherwise set `WEB_PORT`, usually `10000` for Render or `8080` for Hugging Face Spaces) |
| `SESSION_COOKIE_SECURE` | Set to `true` when the public site is served over HTTPS |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Minutes without activity before automatic sign-out (default `15`) |
| `ALLOW_VM_COMMANDS` | Leave unset/false to disable VM shell commands (secure default) |

For an Ollama server deployed separately on Railway, set `OLLAMA_HOST` in the website service's Variables to the Ollama service's private URL (preferred when both services are in the same Railway project) or a secured Cloudflare tunnel URL. Set `OLLAMA_MODEL=qwen2.5:14b` only if that model is pulled/available on the Ollama service. The app uses `qwen2.5:14b` as its default model when `OLLAMA_API_KEY` is not set; when using direct Ollama Cloud, it defaults to `gpt-oss:120b`. Restart/redeploy the website service after changing Variables. Never put the Cloudflare tunnel URL or Ollama API key in browser code.

### Fixing a container startup error about missing secrets

The app intentionally refuses to start unless `SITE_PASSWORD` and `FLASK_SECRET_KEY` are set. They must be configured on the hosting platform; adding them to the Dockerfile or committing them to this repository would expose the website password.

For a Hugging Face Space, open the Space's **Settings → Variables and secrets → Secrets**, add:

1. `SITE_PASSWORD` — the password you chose for entering the website.
2. `FLASK_SECRET_KEY` — a long random value generated once and kept stable across restarts. Generate one locally with `python -c "import secrets; print(secrets.token_hex(32))"` and paste the result as the secret value.
3. `OLLAMA_API_KEY` — the API key from [Ollama API keys](https://ollama.com/settings/keys), if using Ollama Cloud.

Save the secrets and restart/rebuild the Space. The Docker image exposes port `8080`, and the Space metadata uses `app_port: 8080`. Do not paste the secret values into chat, source files, or build logs. For Render, add the same keys under the service's **Environment** settings and redeploy; Render's `PORT` variable overrides the Docker default.

The shared password gives every person who knows it the same access; rotate it if it is shared beyond the intended group.

The site can use Ollama Cloud without hosting an AI model. It still needs network access to the Kali VM if VM commands are enabled. On Render/Spaces, a VM on a private home LAN is normally unreachable; do not expose SSH publicly just to make this work. Keep `ALLOW_VM_COMMANDS` unset unless you have arranged secure private connectivity. If enabling it, also configure `KALI_VM_IP`, `KALI_VM_USER`, and either `KALI_VM_PASS` or `KALI_SSH_KEY`; verify the host key and set `KALI_SSH_KNOWN_HOSTS`. Commands then require the visitor to click a confirmation in the page. The app does not automatically trust unknown VM host keys.
