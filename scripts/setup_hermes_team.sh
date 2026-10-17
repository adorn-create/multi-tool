#!/usr/bin/env bash
set -euo pipefail

if ! command -v hermes >/dev/null 2>&1; then
  printf 'Hermes Agent CLI was not found. Install Hermes and run `hermes setup` first.\n' >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
role_dir="$script_dir/../hermes_team"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
profile_dir="$hermes_home/profiles"

profiles=(
  "security-lead"
  "recon"
  "web-auditor"
  "vulnerability-analyst"
  "report-writer"
)

for profile in "${profiles[@]}"; do
  soul_file="$profile_dir/$profile/SOUL.md"
  marker="<!-- local-kali-ai-team: $profile -->"
  if [[ -e "$profile_dir/$profile" ]] && ! grep -Fqx "$marker" "$soul_file" 2>/dev/null; then
    printf 'Refusing to overwrite existing Hermes profile: %s\n' "$profile" >&2
    exit 1
  fi
done

for profile in "${profiles[@]}"; do
  profile_home="$profile_dir/$profile"
  soul_file="$profile_home/SOUL.md"
  marker="<!-- local-kali-ai-team: $profile -->"
  if [[ -f "$soul_file" ]] && grep -Fqx "$marker" "$soul_file"; then
    printf 'Keeping configured team profile: %s\n' "$profile"
    continue
  fi

  case "$profile" in
    security-lead)
      description="Coordinates authorized security assessments and routes tasks to specialists."
      ;;
    recon)
      description="Maps explicitly authorized targets and identifies their exposed attack surface."
      ;;
    web-auditor)
      description="Reviews provided web application code, configuration, and assessment evidence."
      ;;
    vulnerability-analyst)
      description="Validates reported weaknesses using evidence and non-destructive checks."
      ;;
    report-writer)
      description="Produces clear, evidence-based findings and practical remediation guidance."
      ;;
  esac

  hermes profile create "$profile" --clone --description "$description"
  install -m 600 "$role_dir/$profile.md" "$soul_file"
  printf 'Created team profile: %s\n' "$profile"
done

hermes kanban init
printf '\nTeam profiles and the default Kanban board are ready.\n'
printf 'Start the local dispatcher with `hermes gateway start`.\n'
printf 'Start the website with the local Kali settings in README.md.\n'
