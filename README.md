# Local Kali AI Security Team

This password-protected website runs on your PC and connects over verified SSH to Hermes Agent running inside your Kali VM. The AI profiles, gateway, and Kanban board stay in Kali; the website does not need Hermes installed on Windows and does not require a paid server.

## 1. Set up Hermes in Kali

Install Hermes Agent in Kali using the [official installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation). Run `hermes setup` and configure the active profile with a local model provider. Review its tool permissions and approval mode before continuing.

Copy this repository into Kali or make its `scripts` and `hermes_team` directories available there. Create the specialist profiles and initialize the local Kanban board:

```bash
chmod +x scripts/setup_hermes_team.sh
./scripts/setup_hermes_team.sh
```

The script creates `security-lead`, `recon`, `web-auditor`, `vulnerability-analyst`, and `report-writer` by cloning the active profile, then installs each role prompt. It refuses to overwrite profiles without its setup marker. It does not change profile toolsets, approval settings, or your existing profile. The profiles inherit the active profile's configured model and permissions, so make sure those are appropriate before running the setup.

Start the Hermes gateway in Kali and leave it running while you want the team to work:

```bash
hermes gateway start
```

Find Hermes's absolute executable path and the Kali VM address reachable from your PC:

```bash
command -v hermes
ip -4 addr
```

Enable SSH in Kali and configure a key for the Kali account that owns Hermes. Verify Kali's SSH host key from Kali and add that verified key to the Windows user's OpenSSH `known_hosts` file. The website rejects unknown host keys rather than trusting them automatically. Use a private host-only/bridged VM network, or a VM NAT SSH port-forward bound to the PC's loopback address; do not expose SSH, the Hermes gateway, or the website to the public internet.

## 2. Start the website on your PC

In PowerShell, from the repository directory, create an isolated Python environment and install the dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set the website credentials and the Kali SSH connection in the same PowerShell window. Prefer a key over a VM password:

```powershell
$env:SITE_PASSWORD = Read-Host "Website password"
$env:FLASK_SECRET_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))")
$env:KALI_VM_IP = "192.168.56.101"
$env:KALI_SSH_PORT = "22"
$env:KALI_VM_USER = "kali"
$env:KALI_SSH_KEY = "$env:USERPROFILE\.ssh\id_ed25519"
$env:KALI_SSH_KNOWN_HOSTS = "$env:USERPROFILE\.ssh\known_hosts"
$env:HERMES_CLI = "/home/kali/.local/bin/hermes"
$env:HERMES_KANBAN_BOARD = "default"
$env:HERMES_TEAM_LEAD_PROFILE = "security-lead"
$env:WEB_HOST = "127.0.0.1"
.\.venv\Scripts\python.exe .\ai.py
```

Replace the example VM address, Windows key path, and Hermes path with the values for your setup. For a VM NAT port-forward such as PC `127.0.0.1:2222` to Kali port `22`, set `KALI_VM_IP` to `127.0.0.1` and `KALI_SSH_PORT` to `2222`. If you use password authentication instead, set `KALI_VM_PASS` in the PowerShell environment and leave `KALI_SSH_KEY` empty.

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) on your PC. Binding to loopback keeps the dashboard private to that PC. These environment variables are only needed in the terminal session used to run the website; do not commit credentials to the repository.

The website uses the documented `hermes kanban ... --json` commands over SSH; it does not access Hermes' SQLite database directly. New requests require the authorization checkbox and enter triage assigned to the lead profile. The gateway dispatcher handles tasks and can decompose them for the specialist profiles. The board refreshes every 15 seconds.

The local assessment profiles ask for explicit scope and user approval before active testing. If a task is blocked, inspect it with `hermes kanban show <task-id>` in Kali and only unblock it after reviewing the requested action and confirming it is within scope. The website setup does not enable autonomous/yolo mode or change Hermes approval settings.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `SITE_PASSWORD` | Password for signing in to the website | Required |
| `FLASK_SECRET_KEY` | Flask session signing key | Required |
| `KALI_VM_IP` | VM address reachable from the PC | Required |
| `KALI_SSH_PORT` | Kali SSH port (use the forwarded port for VM NAT) | `22` |
| `KALI_VM_USER` | Kali account that owns the Hermes installation | Required |
| `KALI_SSH_KEY` | Private-key path on the PC | Key or password required |
| `KALI_SSH_KEY_PASSPHRASE` | Passphrase for the private key, if set | Empty |
| `KALI_VM_PASS` | Kali SSH password (prefer a key instead) | Empty |
| `KALI_SSH_KNOWN_HOSTS` | Additional verified SSH host-key file | System known hosts |
| `HERMES_CLI` | Hermes executable path inside Kali | `hermes` |
| `HERMES_KANBAN_BOARD` | Hermes Kanban board slug | `default` |
| `HERMES_TEAM_LEAD_PROFILE` | Profile assigned new assessment tasks | `security-lead` |
| `WEB_HOST` | Interface for the website | `127.0.0.1` |
| `WEB_PORT` / `PORT` | Website port | `8080` |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Idle time before sign-out | `15` |

Use only systems you own or are explicitly authorized to test. Keep the Hermes profile permissions conservative and review blocked work before allowing it to continue.
