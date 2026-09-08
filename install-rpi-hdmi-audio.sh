#!/bin/bash

# 2026-09-08
# Ernst Lanset <ernst.lanser@wobbo.org>
# RPi HDMI Audio v5 - complete installer for Raspberry Pi 5 / 500+ Debian GNOME

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
trap 'printf "\n    Installation failed\n\n"' ERR

clear
printf "\n"
printf "  \033[1mRPi HDMI Audio - Installer\033[0m\n\n"

if [ "$EUID" -ne 0 ]; then
    printf "  Run this installer with sudo:\n"
    printf "  sudo ./install-rpi-hdmi-audio.sh\n\n"
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    printf "  Run this installer from your normal GNOME account with sudo.\n\n"
    exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

user_cmd() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" \
        XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        "$@"
}

printf "    Installing dependencies...\n\n"
apt update -qq
apt install -y -qq \
    python3 python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 \
    libasound2-plugins pulseaudio-utils \
    desktop-file-utils wireplumber

printf "    Removing old experimental ACP / WirePlumber configuration...\n\n"

# Older development versions used custom ALSA Card Profiles and a
# WirePlumber rule. The current application no longer needs them because it
# creates its Stereo / AC-3 sinks directly with module-alsa-sink.
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi0.conf"
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi1.conf"
rm -f "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-rpi-hdmi-ac3.conf"
rm -f "$TARGET_HOME/.config/pipewire/pipewire.conf.d/90-rpi-hdmi-audio-names.conf"

# Remove obsolete per-user autostart entries from development builds. The
# current version uses exactly one system-wide hidden restore entry below.
rm -f "$TARGET_HOME/.config/autostart/rpi-hdmi-audio.desktop"
rm -f "$TARGET_HOME/.config/autostart/rpi-hdmi-audio-restore.desktop"

rmdir --ignore-fail-on-non-empty \
    "$TARGET_HOME/.config/alsa-card-profile/profile-sets" \
    "$TARGET_HOME/.config/alsa-card-profile" \
    "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d" \
    "$TARGET_HOME/.config/pipewire/pipewire.conf.d" \
    2>/dev/null || true

printf "    Preparing saved audio selection...\n\n"

STATE_DIR="$TARGET_HOME/.local/state/rpi-hdmi-audio"
STATE_FILE="$STATE_DIR/last-choice"
mkdir -p "$STATE_DIR"
chown "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.local" "$TARGET_HOME/.local/state" "$STATE_DIR" 2>/dev/null || true

# When upgrading from an older build, preserve the direct sink that is active
# right now if it can be detected. If there is no previous saved state yet and
# no direct sink is active, start with HDMI 0 Stereo 2.0.
if [ -S "/run/user/$TARGET_UID/bus" ]; then
    ACTIVE_SINKS="$(user_cmd pactl list sinks short 2>/dev/null || true)"
    case "$ACTIVE_SINKS" in
        *rpi_hdmi_audio_hdmi0_ac3*)    printf '%s\n' hdmi0-ac3    > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi0_stereo*) printf '%s\n' hdmi0-stereo > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi1_ac3*)    printf '%s\n' hdmi1-ac3    > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi1_stereo*) printf '%s\n' hdmi1-stereo > "$STATE_FILE" ;;
        *)
            if [ ! -s "$STATE_FILE" ]; then
                printf '%s\n' hdmi0-stereo > "$STATE_FILE"
            fi
            ;;
    esac
else
    if [ ! -s "$STATE_FILE" ]; then
        printf '%s\n' hdmi0-stereo > "$STATE_FILE"
    fi
fi
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE"
chmod 600 "$STATE_FILE"

printf "    Installing application...\n\n"
install -m 755 "$SCRIPT_DIR/rpi-hdmi-audio.py" /usr/local/bin/rpi-hdmi-audio

printf "    Installing GNOME launcher...\n\n"
cat > /usr/share/applications/rpi-hdmi-audio.desktop <<'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=RPi HDMI Audio
Comment=Select HDMI stereo or Dolby Digital 5.1 output
Exec=/usr/local/bin/rpi-hdmi-audio
Icon=audio-card
Terminal=false
Categories=Settings;AudioVideo;Audio;
Keywords=audio;hdmi;dolby;ac3;5.1;raspberry;
DESKTOP
chmod 644 /usr/share/applications/rpi-hdmi-audio.desktop

# System-wide GNOME autostart entry. It does NOT open the application window.
# It rebuilds the last successfully selected RPi HDMI Audio sink after login.
# If that fails, the application restores normal HDMI 0 stereo as a fallback.
cat > /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=RPi HDMI Audio Restore
Comment=Restore the last selected RPi HDMI audio mode after login
Exec=/usr/local/bin/rpi-hdmi-audio --restore
Icon=audio-card
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
DESKTOP
chmod 644 /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop

update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

printf "    Restarting the user audio stack with normal configuration...\n\n"
if [ -S "/run/user/$TARGET_UID/bus" ]; then
    user_cmd systemctl --user restart pipewire pipewire-pulse wireplumber || true

    # Wait briefly for pipewire-pulse, then restore HDMI 0 stereo as a safe
    # normal GNOME fallback. The RPi HDMI Audio app will take direct control
    # when the user selects one of its four modes.
    for _ in $(seq 1 30); do
        if user_cmd pactl info >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done

    user_cmd pactl set-card-profile \
        alsa_card.platform-107c701400.hdmi output:hdmi-stereo \
        >/dev/null 2>&1 || true
fi

printf "\n"
printf "  ╔════════════ \033[1mRPi HDMI Audio - Complete\033[0m ════════════╗\n"
printf "  ║  Installation complete.                            ║\n"
printf "  ║  Old experimental ACP/WirePlumber files removed.  ║\n"
printf "  ║  Last selected audio mode is restored after login.   ║\n"
printf "  ╚═════════════════════════════════════════════════════╝\n\n"
