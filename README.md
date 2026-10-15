---
title: Kali AI Assistant
sdk: docker
app_port: 7860
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

Open `http://127.0.0.1:8000`. Create the cloud API key in [Ollama API keys](https://ollama.com/settings/keys). `OLLAMA_API_KEY` selects direct Ollama Cloud by default; the app sends it only from the server to `https://ollama.com`. Use a model name supported by the cloud API, such as `gpt-oss:120b`, and set `OLLAMA_MODEL` to choose another. For local Ollama instead, omit `OLLAMA_API_KEY` and set `OLLAMA_HOST=http://localhost:11434`.

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
| `PORT` / `WEB_PORT` | Platform port (`PORT` is detected automatically; otherwise set `WEB_PORT`, usually `10000` for Render or `7860` for Hugging Face Spaces) |
| `SESSION_COOKIE_SECURE` | Set to `true` when the public site is served over HTTPS |
| `ALLOW_VM_COMMANDS` | Leave unset/false to disable VM shell commands (secure default) |

For Render, create a **Docker** web service from this repository and set its environment variables in the Render dashboard. For Hugging Face Spaces, create a **Docker Space**, add the secrets under Space Settings, and set `WEB_PORT=7860`. In either case, generate `FLASK_SECRET_KEY` with a cryptographically random value. The shared password gives every person who knows it the same access; rotate it if it is shared beyond the intended group.

The site can use Ollama Cloud without hosting an AI model. It still needs network access to the Kali VM if VM commands are enabled. On Render/Spaces, a VM on a private home LAN is normally unreachable; do not expose SSH publicly just to make this work. Keep `ALLOW_VM_COMMANDS` unset unless you have arranged secure private connectivity. If enabling it, also configure `KALI_VM_IP`, `KALI_VM_USER`, and either `KALI_VM_PASS` or `KALI_SSH_KEY`; verify the host key and set `KALI_SSH_KNOWN_HOSTS`. Commands then require the visitor to click a confirmation in the page. The app does not automatically trust unknown VM host keys.
