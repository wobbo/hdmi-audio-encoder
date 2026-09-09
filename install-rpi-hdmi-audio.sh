#!/bin/bash

# 2026-09-09
# Ernst Lanser <ernst.lanser@wobbo.org>
# HDMI Audio Encoder v6 - installer for Raspberry Pi 5 / 500+ Debian 13 GNOME

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

BUILD_DIR=""
cleanup() {
    if [ -n "$BUILD_DIR" ] && [ -d "$BUILD_DIR" ]; then
        rm -rf "$BUILD_DIR"
    fi
}
trap cleanup EXIT
trap 'printf "\n    Installation failed\n\n"' ERR

clear
printf "\n"
printf "  \033[1mHDMI Audio Encoder - Installer\033[0m\n\n"

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
PROJECT_STATE_DIR="/var/lib/hdmi-audio-encoder"
DCAENC_COMMIT="68ed0d6d370268f04c22cadc9c8fc54a479958ab"

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
    libasound2-plugins libasound2-dev \
    pulseaudio-utils desktop-file-utils wireplumber \
    git build-essential autoconf automake libtool pkg-config

mkdir -p "$PROJECT_STATE_DIR"
chmod 755 "$PROJECT_STATE_DIR"

# ---------------------------------------------------------------------------
# DTS / dcaenc
#
# Debian provides the ALSA A52 encoder used for Dolby Digital, but does not
# ship the old dcaenc ALSA plugin that this application uses for realtime DTS.
# Build the known upstream revision when the plugin is not already present.
#
# The ALSA library directory is discovered through pkg-config rather than
# hard-coded, which keeps this part usable on arm64 and amd64.
# ---------------------------------------------------------------------------

ALSA_LIBDIR="$(pkg-config --variable=libdir alsa)"
DCA_PLUGIN="$ALSA_LIBDIR/alsa-lib/libasound_module_pcm_dca.so"
DCA_CONF="/usr/share/alsa/pcm/dca.conf"
printf '%s\n' "$ALSA_LIBDIR" > "$PROJECT_STATE_DIR/dca-libdir"

DCA_MANAGED=0
if [ -f "$PROJECT_STATE_DIR/dcaenc-installed-by-hdmi-audio-encoder" ]; then
    DCA_MANAGED=1
fi

if [ -f "$DCA_PLUGIN" ] && [ -f "$DCA_CONF" ]; then
    printf "    Existing DTS/dcaenc ALSA plugin found; reusing it.\n\n"
else
    printf "    Building DTS/dcaenc ALSA plugin...\n\n"

    BUILD_DIR="$(mktemp -d /tmp/hdmi-audio-encoder-dcaenc.XXXXXX)"

    git clone -q https://github.com/darealshinji/dcaenc.git "$BUILD_DIR/dcaenc"
    git -C "$BUILD_DIR/dcaenc" checkout -q "$DCAENC_COMMIT"

    cd "$BUILD_DIR/dcaenc"

    # Modern libtool needs the auxiliary directory to be explicit for this
    # 2014 autotools project.
    if ! grep -q '^AC_CONFIG_AUX_DIR' configure.ac; then
        sed -i \
            '/AC_CONFIG_HEADERS(\[config.h\])/a AC_CONFIG_AUX_DIR([.])' \
            configure.ac
    fi

    autoreconf -f -i -v
    ./configure --prefix=/usr --libdir="$ALSA_LIBDIR"
    make -j"$(nproc)"
    make install
    ldconfig

    touch "$PROJECT_STATE_DIR/dcaenc-installed-by-hdmi-audio-encoder"
    DCA_MANAGED=1

    cd "$SCRIPT_DIR"
fi

if [ ! -f "$DCA_PLUGIN" ] || [ ! -f "$DCA_CONF" ]; then
    printf "  DTS installation did not create the expected ALSA files.\n\n"
    exit 1
fi

# If dcaenc existed before this installer, preserve its original ALSA config
# so the remover can undo only our IEC61937 compatibility change.
if [ "$DCA_MANAGED" -eq 0 ] && \
   [ ! -f "$PROJECT_STATE_DIR/dca.conf.before-hdmi-audio-encoder" ]; then
    cp -a "$DCA_CONF" "$PROJECT_STATE_DIR/dca.conf.before-hdmi-audio-encoder"
fi

# Upstream dca.conf defines IEC61937 but omits it from the dcahdmi argument
# lists. Add it so dcahdmi:...,IEC61937=1 can be selected by the application.
sed -i \
    's/@args \[ CARD DEV AES0 AES1 AES2 AES3 \]/@args [ CARD DEV AES0 AES1 AES2 AES3 IEC61937 ]/' \
    "$DCA_CONF"

if [ "$(grep -c '@args \[ CARD DEV AES0 AES1 AES2 AES3 IEC61937 \]' "$DCA_CONF" || true)" -lt 2 ]; then
    printf "  Could not enable IEC61937 in %s.\n\n" "$DCA_CONF"
    exit 1
fi

# dca.conf is not loaded automatically by ALSA. Add one include to this user's
# .asoundrc and remember whether this installer added it, so removal is safe.
ASOUNDRC="$TARGET_HOME/.asoundrc"
if ! grep -qxF '<confdir:pcm/dca.conf>' "$ASOUNDRC" 2>/dev/null; then
    printf '\n<confdir:pcm/dca.conf>\n' >> "$ASOUNDRC"
    chown "$TARGET_USER:$TARGET_USER" "$ASOUNDRC"
    touch "$PROJECT_STATE_DIR/asoundrc-include-added"
fi

printf "    Removing old experimental ACP / WirePlumber configuration...\n\n"

# Older development versions used custom ALSA Card Profiles and a
# WirePlumber rule. The current application no longer needs them because it
# creates its Stereo / AC-3 / DTS sinks directly with module-alsa-sink.
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

# When upgrading, preserve the direct sink that is active right now if it can
# be detected. If there is no previous saved state and no direct sink is
# active, start with HDMI 0 Stereo 2.0.
if [ -S "/run/user/$TARGET_UID/bus" ]; then
    ACTIVE_SINKS="$(user_cmd pactl list sinks short 2>/dev/null || true)"
    case "$ACTIVE_SINKS" in
        *rpi_hdmi_audio_hdmi0_dts*)    printf '%s\n' hdmi0-dts    > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi0_ac3*)    printf '%s\n' hdmi0-ac3    > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi0_stereo*) printf '%s\n' hdmi0-stereo > "$STATE_FILE" ;;
        *rpi_hdmi_audio_hdmi1_dts*)    printf '%s\n' hdmi1-dts    > "$STATE_FILE" ;;
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
Name=HDMI Audio Encoder
Comment=Select HDMI stereo, Dolby Digital 5.1, or DTS 5.1 output
Exec=/usr/local/bin/rpi-hdmi-audio
Icon=audio-card
Terminal=false
Categories=Settings;AudioVideo;Audio;
Keywords=audio;hdmi;dolby;ac3;dts;surround;5.1;raspberry;
DESKTOP
chmod 644 /usr/share/applications/rpi-hdmi-audio.desktop

# System-wide GNOME autostart entry. It does NOT open the application window.
# It rebuilds the last successfully selected HDMI Audio Encoder sink after
# login. If that fails, the application restores normal HDMI 0 stereo.
cat > /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=HDMI Audio Encoder Restore
Comment=Restore the last selected HDMI audio mode after login
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
    # normal GNOME fallback. The app takes direct control when the user selects
    # one of its six modes.
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
printf "  ╔════════════ \033[1mHDMI Audio Encoder - Complete\033[0m ═══════════╗\n"
printf "  ║  Installation complete.                            ║\n"
printf "  ║  Stereo, Dolby Digital 5.1 and DTS 5.1 available. ║\n"
printf "  ║  Last selected audio mode is restored after login.║\n"
printf "  ╚═════════════════════════════════════════════════════╝\n\n"
