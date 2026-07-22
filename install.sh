#!/usr/bin/env bash
set -euo pipefail

source_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
repo_root="$(cd -- "$(dirname -- "$source_path")" && pwd -P)"

if [[ -n "${CODEX_SKILLS_DIR:-}" ]]; then
  skills_root="${CODEX_SKILLS_DIR}"
elif [[ -n "${CODEX_HOME:-}" ]]; then
  skills_root="${CODEX_HOME}/skills"
elif [[ -d "${HOME}/.agents/skills" ]]; then
  skills_root="${HOME}/.agents/skills"
elif [[ -d "${HOME}/.codex/skills" ]]; then
  skills_root="${HOME}/.codex/skills"
else
  skills_root="${HOME}/.agents/skills"
fi

skill_source="${repo_root}/skills/ciduxx"
skill_target="${skills_root}/ciduxx"
bin_root="${HOME}/.local/bin"
bin_source="${repo_root}/bin/ciduxx"
bin_target="${bin_root}/ciduxx"

mkdir -p -- "$skills_root" "$bin_root"

install_link() {
  local source="$1"
  local target="$2"
  if [[ -L "$target" && "$(readlink -f -- "$target")" == "$(readlink -f -- "$source")" ]]; then
    return
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    printf 'ciduxx: refusing to replace existing path: %s\n' "$target" >&2
    exit 1
  fi
  ln -s -- "$source" "$target"
}

install_link "$skill_source" "$skill_target"
install_link "$bin_source" "$bin_target"

printf 'Installed skill: %s\n' "$skill_target"
printf 'Installed launcher: %s\n' "$bin_target"
if [[ ":${PATH}:" != *":${bin_root}:"* ]]; then
  printf 'Add %s to PATH to run the ciduxx launcher.\n' "$bin_root"
fi
