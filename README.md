---
title: Kali AI Assistant
sdk: docker
app_port: 8080
---

# Kali AI Assistant website

This project provides a password-protected website for the Kali VM AI assistant. It asks Hermes3 and Qwen2.5:14b for independent responses, then uses Hermes3 to synthesize a unified answer.

## Start locally

Install dependencies, then set the required secrets and start the server:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SITE_PASSWORD = Read-Host "Enter the shared website password"
$env:FLASK_SECRET_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))")
$env:OLLAMA_HOST = "http://localhost:11434"
$env:KALI_VM_IP = "your-vm-address"
$env:KALI_VM_USER = "your-vm-username"
.\.venv\Scripts\python.exe .\ai.py
```

Open `http://127.0.0.1:8080`. Pull both models onto the Ollama server before starting the website:

```sh
ollama pull hermes3
ollama pull qwen2.5:14b
```

`OLLAMA_HOST` must point to an Ollama server with both models available; use `http://localhost:11434` for local Ollama or the reachable service URL for a separate host.

To show the Kali package catalog, set `ALLOW_TOOL_INSTALLS=true` in the website environment. The VM SSH user needs passwordless `sudo` access; leaving the variable unset keeps the installer hidden and disabled.

## Public hosting

The Dockerfile works with Render and Hugging Face Spaces Docker. Configure these deployment secrets/environment variables in the host dashboard, not in source code:

| Variable | Purpose |
| --- | --- |
| `SITE_PASSWORD` | Shared password visitors enter to access the website |
| `FLASK_SECRET_KEY` | Unique random secret used to sign sessions; keep it stable across restarts |
| `OLLAMA_API_KEY` | Optional. Only needed for direct requests to `https://ollama.com`; never add it to browser code or commit it |
| `CF_ACCESS_CLIENT_ID` | Optional Cloudflare Access Service Auth token ID for a protected custom Ollama tunnel |
| `CF_ACCESS_CLIENT_SECRET` | Optional matching Cloudflare Access Service Auth token secret |
| `OLLAMA_HOST` | Base URL of the Ollama server that contains both required models |
| `WEB_HOST` | Set to `0.0.0.0` in the container platform |
| `PORT` / `WEB_PORT` | Platform port (`PORT` is detected automatically; otherwise set `WEB_PORT`, usually `10000` for Render or `8080` for Hugging Face Spaces) |
| `SESSION_COOKIE_SECURE` | Set to `true` when the public site is served over HTTPS |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Minutes without activity before automatic sign-out (default `15`) |
| `ALLOW_VM_COMMANDS` | Leave unset/false to disable VM shell commands (secure default) |
| `ALLOW_TOOL_INSTALLS` | Set to `true` to enable the curated Kali package installer; does not enable AI shell commands |

When `ALLOW_TOOL_INSTALLS=true`, the signed-in website shows a curated catalog of standard Kali packages (`nmap`, `tcpdump`, `wireshark`, `dnsutils`, `whois`, `net-tools`, `git`, and `python3-venv`). Selecting **Install** requires confirmation and installs only that allowlisted package from the VM's configured APT repositories; arbitrary package names and download URLs are not accepted. The configured VM user must have passwordless `sudo` access for `apt-get update` and package installation. This installer has its own opt-in and does not enable AI-generated VM commands. Keep the website private to trusted users and only connect it to a VM you own or are authorized to administer.

For an Ollama server deployed separately on Railway, **no Ollama API key is needed**. Pull both models on the machine/container running Ollama:

```sh
ollama pull hermes3
ollama pull qwen2.5:14b
```

The website asks both models for a response, then Hermes3 synthesizes their answers. It will only prepare a VM command if both models independently return the exact same command; otherwise it responds without preparing or running a command. Set `OLLAMA_HOST` in the website service's Variables to the Ollama service's private URL (preferred when both services are in the same Railway project), then redeploy the website. Do not set `OLLAMA_API_KEY` for this self-hosted setup. The app only sends that optional key when connecting to the official `ollama.com` host; it never forwards it to Railway or a custom tunnel.

If the tunnel is protected by Cloudflare Access, create a **Service Auth** service token in Cloudflare Access and add its client ID and client secret as `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in the website host's secret settings. The website sends these headers only to the configured custom Ollama endpoint; it does not send the Ollama Cloud API key to that endpoint. Both Cloudflare variables must be set together. For a public Quick Tunnel that does not use Cloudflare Access, leave both unset and check the Ollama/proxy logs and authentication configuration for the source of a `401`.

Direct Ollama Cloud at `https://ollama.com` is a separate option and requires an Ollama API key. Without a key, connect to your own Ollama server using the Railway setup above.

### Fixing a container startup error about missing secrets

The app intentionally refuses to start unless `SITE_PASSWORD` and `FLASK_SECRET_KEY` are set. They must be configured on the hosting platform; adding them to the Dockerfile or committing them to this repository would expose the website password.

For a Hugging Face Space, open the Space's **Settings → Variables and secrets → Secrets**, add:

1. `SITE_PASSWORD` — the password you chose for entering the website.
2. `FLASK_SECRET_KEY` — a long random value generated once and kept stable across restarts. Generate one locally with `python -c "import secrets; print(secrets.token_hex(32))"` and paste the result as the secret value.
3. `OLLAMA_API_KEY` — the API key from [Ollama API keys](https://ollama.com/settings/keys), if using Ollama Cloud.

Save the secrets and restart/rebuild the Space. The Docker image exposes port `8080`, and the Space metadata uses `app_port: 8080`. Do not paste the secret values into chat, source files, or build logs. For Render, add the same keys under the service's **Environment** settings and redeploy; Render's `PORT` variable overrides the Docker default.

The shared password gives every person who knows it the same access; rotate it if it is shared beyond the intended group.

The site can use Ollama Cloud without hosting an AI model. It still needs network access to the Kali VM if VM commands are enabled. On Render/Spaces, a VM on a private home LAN is normally unreachable; do not expose SSH publicly just to make this work. Keep `ALLOW_VM_COMMANDS` unset unless you have arranged secure private connectivity. If enabling it, also configure `KALI_VM_IP`, `KALI_VM_USER`, and either `KALI_VM_PASS` or `KALI_SSH_KEY`; verify the host key and set `KALI_SSH_KNOWN_HOSTS`. Commands then require the visitor to click a confirmation in the page. The app does not automatically trust unknown VM host keys.
