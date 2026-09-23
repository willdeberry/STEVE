#!/bin/bash
# Small per-user installer for macOS. The complete STEVE payload sits next to this script.
set -euo pipefail

ADDINS="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
MARKER="STEVE managed installation"

fail() {
  printf 'Installation did not finish. %s\n' "$1" >&2
  exit 1
}

safe_relative_path() {
  # Reject manifest entries that could leave the payload folder.
  case "$1" in
    ""|/*|..|../*|*/..|*/../*) return 1 ;;
  esac
  return 0
}

verify_payload() {
  local source="$1" sums="$2" line digest relative actual
  if [[ ! -f "$sums" || ! -f "$source/STEVE.manifest" ]]; then
    fail "Extract the entire STEVE zip before running the installer."
  fi
  [[ -s "$sums" ]] || fail "The package manifest is empty."
  # Validate every listed file before changing the installation.
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ ${#line} -lt 67 || "${line:64:2}" != "  " ]]; then
      fail "Invalid checksum manifest."
    fi
    digest="${line:0:64}"
    relative="${line:66}"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || fail "Invalid checksum manifest."
    safe_relative_path "$relative" || fail "The package contains an invalid path."
    [[ -f "$source/$relative" ]] || fail "Package verification failed. Download STEVE again."
    actual="$(shasum -a 256 "$source/$relative" | cut -d ' ' -f 1)"
    [[ "$actual" == "$digest" ]] || fail "Package verification failed. Download STEVE again."
  done < "$sums"
}

previous_installation() {
  local root="$1" folder name manifest marker found=""
  for folder in "$root"/*; do
    [[ -d "$folder" ]] || continue
    name="$(basename "$folder")"
    [[ "$name" != STEVE && "$name" =~ ^[A-Za-z][A-Za-z0-9_-]{0,79}$ ]] || continue
    manifest="$folder/$name.manifest"
    marker="$folder/$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')-install-marker.txt"
    [[ -f "$manifest" && -f "$marker" && -f "$folder/$name.py" ]] || continue
    [[ "$(cat "$marker")" == "$name managed installation" ]] || continue
    [[ "$(plutil -extract author raw -o - "$manifest" 2>/dev/null)" == "10-X-eng" ]] || continue
    [[ "$(plutil -extract type raw -o - "$manifest" 2>/dev/null)" == "addin" ]] || continue
    [[ "$(plutil -extract autodeskProduct raw -o - "$manifest" 2>/dev/null)" == "Fusion" ]] || continue
    plutil -extract description json -o - "$manifest" 2>/dev/null | grep -Fq 'Engineering & Visualization Expert' || continue
    [[ ! -L "$folder" ]] || fail "Installation folders must not be filesystem links."
    [[ -z "$found" ]] || fail "Multiple previous installations were found. Keep only the one you want to upgrade."
    found="$folder"
  done
  printf '%s' "$found"
}

install_payload() {
  local package="$1" root="$2"
  local source="$package/STEVE" sums="$package/SHA256SUMS"
  verify_payload "$source" "$sums"
  mkdir -p "$root" || fail "The add-in directory could not be created."
  [[ ! -L "$root" ]] || fail "Installation folders must not be filesystem links."
  local previous
  previous="$(previous_installation "$root")" || return 1
  local destination="$root/STEVE"
  [[ ! -L "$destination" ]] || fail "Installation folders must not be filesystem links."
  local staging="$root/STEVE-staging-$(uuidgen | tr -d '-')"
  local backup_root
  backup_root="$(dirname "$root")/STEVE-install-backups"
  local backup="$backup_root/$(date -u +%Y%m%d-%H%M%S)-$(uuidgen | tr -d '-')"
  if [[ -d "$destination" && ! -f "$destination/steve-install-marker.txt" ]]; then
    fail "An unmanaged STEVE folder already exists. Rename it before installing."
  fi
  [[ -z "$previous" || ! -d "$destination" ]] || fail "Both a previous installation and STEVE exist. Keep only the add-in you want to upgrade; saved data has not been changed."
  mkdir -p "$staging" || fail "The staging directory could not be created."
  local line relative
  while IFS= read -r line || [[ -n "$line" ]]; do
    relative="${line:66}"
    mkdir -p "$staging/$(dirname "$relative")" || fail "The package could not be staged."
    cp "$source/$relative" "$staging/$relative" || fail "The package could not be staged."
  done < "$sums"
  # Zip extraction can drop Unix permission bits; the bundled runtime must stay executable.
  local binary
  for binary in "$staging"/runtime/bin/* "$staging"/runtime/codex-path/* "$staging"/runtime/codex-resources/zsh/bin/*; do
    if [[ -f "$binary" ]]; then
      chmod 755 "$binary" || fail "The runtime could not be made executable."
    fi
  done
  # The payload was verified above; the download quarantine flag is no longer needed.
  xattr -dr com.apple.quarantine "$staging" 2>/dev/null || true
  printf '%s' "$MARKER" > "$staging/steve-install-marker.txt" || fail "The installation marker could not be saved."
  if [[ -n "$previous" ]]; then
    printf '{"previousName":"%s"}' "$(basename "$previous")" > "$staging/steve-upgrade.json" || fail "The upgrade record could not be saved."
  elif [[ -f "$destination/steve-upgrade.json" ]]; then
    cp "$destination/steve-upgrade.json" "$staging/steve-upgrade.json" || fail "The upgrade record could not be preserved."
  fi
  local replaced="${previous:-$destination}"
  local helper_staged="$staging/STEVEUpdater"
  local helper_source="$destination/STEVEUpdater"
  local helper_destination="$root/STEVEUpdater"
  local helper_backup="$backup_root/STEVEUpdater-$(date -u +%Y%m%d-%H%M%S)-$(uuidgen | tr -d '-')"
  local helper_backed_up=0 helper_moved=0
  # Validate the migration helper while the verified staging tree is still
  # intact. A malformed helper must not move the old installation first.
  if [[ -e "$helper_staged" ]]; then
    [[ ! -L "$helper_staged" && -f "$helper_staged/STEVEUpdater.py" && -f "$helper_staged/STEVEUpdater.manifest" ]] || fail "The STEVEUpdater helper is incomplete."
  fi
  rollback_transaction() {
    if [[ "$helper_moved" == 1 && -e "$helper_destination" ]]; then
      rm -rf "$helper_destination"
    fi
    if [[ "$helper_backed_up" == 1 && -e "$helper_backup" && ! -e "$helper_destination" ]]; then
      mv "$helper_backup" "$helper_destination" || true
    fi
    if [[ -e "$destination" ]]; then
      rm -rf "$destination"
    fi
    if [[ -d "$backup" && ! -e "$replaced" ]]; then
      mv "$backup" "$replaced" || true
    fi
  }
  # Fusion can reopen after the outer wait; do not swap a live add-in.
  if pgrep -x "Autodesk Fusion" > /dev/null; then
    fail "Fusion reopened during installation. Quit Fusion and retry."
  fi
  if [[ -d "$replaced" ]]; then
    mkdir -p "$backup_root" || fail "The backup directory could not be created."
    [[ ! -L "$backup_root" ]] || fail "Installation folders must not be filesystem links."
    mv "$replaced" "$backup" || fail "The previous add-in could not be backed up."
  fi
  if pgrep -x "Autodesk Fusion" > /dev/null; then
    if [[ -d "$backup" && ! -d "$replaced" ]]; then
      mv "$backup" "$replaced" || fail "Fusion reopened and the old add-in could not be restored; check the backup folder."
    fi
    fail "Fusion reopened during installation. Quit Fusion and retry."
  fi
  if ! mv "$staging" "$destination"; then
    rollback_transaction
    fail "STEVE could not be moved into place."
  fi

  # The migration payload carries the lifecycle helper inside the verified
  # archive, but Fusion must load it as a separate sibling add-in.
  if [[ -d "$helper_source" ]]; then
    mkdir -p "$backup_root" || { rollback_transaction; fail "The backup directory could not be created."; }
    [[ ! -L "$helper_destination" ]] || { rollback_transaction; fail "The STEVEUpdater installation is a filesystem link."; }
    if [[ -e "$helper_destination" ]]; then
      if ! mv "$helper_destination" "$helper_backup"; then
        rollback_transaction
        fail "The previous STEVEUpdater could not be backed up."
      fi
      helper_backed_up=1
    fi
    if ! mv "$helper_source" "$helper_destination"; then
      rollback_transaction
      fail "The STEVEUpdater helper could not be installed."
    fi
    helper_moved=1
  fi
}

# Test mode requires an explicit destination and does not touch Fusion's installation.
if [[ $# -eq 3 && "$1" == "--test-install" ]]; then
  if install_payload "$2" "$3" 2> "$2/installer-test-error.txt"; then
    rm -f "$2/installer-test-error.txt"
    exit 0
  fi
  exit 1
fi

# Detached by STEVE after the user chooses Install update; no Terminal window.
if [[ $# -eq 2 && "$1" == "--auto-install" ]]; then
  result="$2"
  package="$(cd "$(dirname "$0")" && pwd)"
  printf 'Waiting for Fusion to close. Save your work and quit Fusion.\n' > "$result"
  while pgrep -x "Autodesk Fusion" > /dev/null; do sleep 2; done
  if (install_payload "$package" "$ADDINS" >> "$result" 2>&1); then
    printf 'Installed. Reopen Fusion to use the update.\n' > "$result"
  else
    printf 'Installation failed; the previous installation was retained if the replacement failed.\n' >> "$result"
    exit 1
  fi
  exit 0
fi

package="$(cd "$(dirname "$0")" && pwd)"
printf '\nMeet STEVE.\nYour engineering partner inside Fusion.\nInstall STEVE, sign in with ChatGPT, and start a conversation.\n\n'
printf 'Installs for your macOS account. No administrator access needed.\n'
while pgrep -x "Autodesk Fusion" > /dev/null; do
  printf '\nFusion is still running. Save your work and quit Fusion, then press Return to continue (Control-C cancels).\n'
  read -r _
done
printf '\nChecking the package and installing STEVE…\n'
install_payload "$package" "$ADDINS"
printf '\nInstalled. Open Fusion, then choose STEVE in the Quick Access toolbar.\n'
printf 'If needed, enable STEVE under Scripts and Add-ins first.\n\nYou can close this window.\n'
