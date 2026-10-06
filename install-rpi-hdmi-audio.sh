#!/usr/bin/env bash
#
# HDMI Audio Encoder 16.5 installer
# Raspberry Pi OS / Debian 13 GNOME
#
# This installer:
#   1. validates the desktop user and the matching v16.5 Python application;
#   2. installs GTK, PipeWire, WirePlumber, ALSA and build dependencies;
#   3. installs or reuses the DTS/dcaenc ALSA plugin;
#   4. enables the IEC61937 DTS path used by the application;
#   5. cleans only files created by older versions of this project;
#   6. preserves saved Volume and Frequency Balance settings on upgrades;
#   7. selects PCM (Default) on a completely fresh installation;
#   8. installs the application, GNOME launcher and login restore helper;
#   9. reloads the user audio stack and restores the selected audio mode.
#
# PCM (Default) does not create a forced 2.0 or 5.1 ALSA sink.
# It returns the selected HDMI port to the normal PipeWire/WirePlumber path.
# Native PCM 5.1 is preferred only when HDMI/ELD advertises six-channel LPCM;
# otherwise the normal native HDMI stereo profile is used.
#
# Run this file from the normal logged-in GNOME account:
#   sudo ./install-rpi-hdmi-audio.sh
#
set -euo pipefail

# Prevent package installation from opening interactive configuration dialogs.
export DEBIAN_FRONTEND=noninteractive

# Root access is required for system files, but SUDO_USER must still identify
# the normal GNOME user whose PipeWire session and per-user settings we manage.
if (( EUID != 0 )) || [[ -z ${SUDO_USER:-} || ${SUDO_USER:-} == root ]]; then
    printf 'Run this installer from your normal desktop account with sudo.\n' >&2
    exit 1
fi

# Resolve the desktop user's numeric IDs and home directory once. These values
# are reused whenever a command must run inside that user's desktop session.
TARGET_USER=$SUDO_USER
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_GID=$(id -g "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -d $TARGET_HOME && $TARGET_HOME == /* ]] || { echo 'Desktop user home directory not found.' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
APP_SOURCE="$SCRIPT_DIR/rpi-hdmi-audio.py"
APP=/usr/local/bin/rpi-hdmi-audio
PROJECT_STATE=/var/lib/hdmi-audio-encoder
USER_META="$PROJECT_STATE/users/$TARGET_UID"
DCA_COMMIT=68ed0d6d370268f04c22cadc9c8fc54a479958ab
DCA_CONF=/usr/share/alsa/pcm/dca.conf
ALSA_INCLUDE=/etc/alsa/conf.d/60-dca-encoder.conf
# BUILD_DIR is empty unless dcaenc must be compiled. The EXIT trap removes the
# temporary source tree on both success and failure.
BUILD_DIR=
trap '[[ -z $BUILD_DIR ]] || rm -rf -- "$BUILD_DIR"' EXIT

# Run a command as the desktop user with the correct runtime and DBus paths.
# This is required for pactl and systemctl --user to talk to that login session.
user_cmd() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" "$@"
}

# Return success only when the user's DBus socket exists and PipeWire's
# PulseAudio-compatible control interface answers.
session_live() {
    [[ -S /run/user/$TARGET_UID/bus ]] && user_cmd pactl info >/dev/null 2>&1
}

# The installer and Python application must be downloaded into the same folder.
[[ -f $APP_SOURCE ]] || { printf 'Missing file: %s\n' "$APP_SOURCE" >&2; exit 1; }
if [[ -e $ALSA_INCLUDE ]] && \
   [[ $(cat -- "$ALSA_INCLUDE") != '<confdir:pcm/dca.conf>' ]]; then
    printf 'Existing %s has unexpected contents; installation stopped.\n' "$ALSA_INCLUDE" >&2
    exit 1
fi

# Refresh APT metadata, then install the runtime and build dependencies.
# GTK4/libadwaita provide the GUI, PipeWire/WirePlumber provide desktop audio,
# ALSA provides HDMI/codec access, and the build tools are only needed when the
# DTS plugin has to be compiled locally.
printf 'Installing HDMI Audio Encoder 16.5...\n'
apt-get update -qq
apt-get install -y -qq \
    python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    libasound2-plugins libasound2-dev alsa-utils pulseaudio-utils \
    desktop-file-utils pipewire pipewire-bin wireplumber \
    git build-essential autoconf automake libtool pkg-config

# Parse the Python source before installing it. This prevents accidentally
# pairing a v16.5 installer with an older application file.
python3 - "$APP_SOURCE" <<'PY'
import ast
import pathlib
import sys
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
version = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION'
                        for t in n.targets) and isinstance(n.value, ast.Constant)), None)
revision = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'DSP_CONFIG_REVISION'
                         for t in n.targets) and isinstance(n.value, ast.Constant)), None)
if version != '16.5' or revision != '16.5-pcm-default':
    raise SystemExit('rpi-hdmi-audio.py must be the matching v16.5 build.')
PY

# Create root-owned project metadata directories. They store only installation
# ownership/restoration information, not the user's live audio configuration.
install -d -m 700 "$PROJECT_STATE" "$PROJECT_STATE/users" "$USER_META"

# Only a fresh installation can tell us the user's actual pre-app audio state.
# Never replace this snapshot during an upgrade.
if [[ ! -e $APP && ! -f $USER_META/audio-before-install ]] && session_live; then
    default_sink=$(user_cmd pactl get-default-sink 2>/dev/null || true)
    if [[ $default_sink != rpi_hdmi_audio_* ]]; then
        card_profiles=$(user_cmd pactl list cards 2>/dev/null | awk '
            /^[[:space:]]*Name: / { name=$2 }
            /^[[:space:]]*Active Profile: / {
                if (name == "alsa_card.platform-107c701400.hdmi") print "card0=" $3
                if (name == "alsa_card.platform-107c706400.hdmi") print "card1=" $3
            }')
        {
            printf '%s\n' "$card_profiles"
            printf 'default=%s\n' "$default_sink"
        } > "$USER_META/audio-before-install"
        chmod 600 "$USER_META/audio-before-install"
        if user_cmd systemctl --user is-active --quiet filter-chain.service; then
            : > "$USER_META/filter-chain-was-active"
        fi
    fi
fi

# Ask pkg-config for the architecture-correct ALSA library directory instead of
# hard-coding an arm64 path. This also keeps the script usable on amd64.
ALSA_LIBDIR=$(pkg-config --variable=libdir alsa)
[[ $ALSA_LIBDIR == /* && $ALSA_LIBDIR != *..* ]] || { echo 'Invalid ALSA library path.' >&2; exit 1; }
DCA_PLUGIN="$ALSA_LIBDIR/alsa-lib/libasound_module_pcm_dca.so"
printf '%s\n' "$ALSA_LIBDIR" > "$PROJECT_STATE/dca-libdir"

if [[ ! -f $DCA_PLUGIN || ! -f $DCA_CONF ]]; then
    if [[ ! -f $PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder ]]; then
        # Building over a partially installed third-party dcaenc would erase
        # its files and make safe removal impossible.
        for existing in "$DCA_PLUGIN" "$DCA_CONF" /usr/bin/dcaenc \
            /usr/include/dcaenc.h "$ALSA_LIBDIR/libdcaenc.so" \
            "$ALSA_LIBDIR/libdcaenc.so.0" \
            "$ALSA_LIBDIR/libdcaenc.so.0.0.0" \
            "$ALSA_LIBDIR/libdcaenc.la" \
            "$ALSA_LIBDIR/alsa-lib/libasound_module_pcm_dca.la" \
            "$ALSA_LIBDIR/pkgconfig/dcaenc.pc"; do
            if [[ -e $existing || -L $existing ]]; then
                echo 'An incomplete existing DTS installation was found; repair it manually first.' >&2
                exit 1
            fi
        done
    fi
    printf 'Building the DTS/dcaenc ALSA plugin...\n'
    BUILD_DIR=$(mktemp -d /tmp/hdmi-audio-encoder-dcaenc.XXXXXXXX)
    # Build one known upstream dcaenc revision so later releases do not silently
    # change behaviour underneath this application.
    git clone -q https://github.com/darealshinji/dcaenc.git "$BUILD_DIR/dcaenc"
    git -C "$BUILD_DIR/dcaenc" checkout -q "$DCA_COMMIT"
    pushd "$BUILD_DIR/dcaenc" >/dev/null
    if ! grep -q '^AC_CONFIG_AUX_DIR' configure.ac; then
        sed -i '/AC_CONFIG_HEADERS(\[config.h\])/a AC_CONFIG_AUX_DIR([.])' configure.ac
    fi
    autoreconf -f -i -v
    ./configure --prefix=/usr --libdir="$ALSA_LIBDIR"
    make -j"$(nproc)"
    # Track ownership before the first installed file, also on partial errors.
    : > "$PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder"
    make install
    popd >/dev/null
    ldconfig
fi
[[ -f $DCA_PLUGIN && -f $DCA_CONF ]] || { echo 'DTS installation is incomplete.' >&2; exit 1; }

# An existing, independently installed dcaenc config must return byte-for-byte.
if [[ ! -f $PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder && \
      ! -e $PROJECT_STATE/dca.conf.before-hdmi-audio-encoder ]]; then
    cp -a -- "$DCA_CONF" "$PROJECT_STATE/dca.conf.before-hdmi-audio-encoder"
fi
sed -i 's/@args \[ CARD DEV AES0 AES1 AES2 AES3 \]/@args [ CARD DEV AES0 AES1 AES2 AES3 IEC61937 ]/' "$DCA_CONF"
(( $(grep -cF '@args [ CARD DEV AES0 AES1 AES2 AES3 IEC61937 ]' "$DCA_CONF" || true) >= 2 )) || {
    echo 'DTS configuration does not contain the expected IEC61937 arguments.' >&2
    exit 1
}

# Make the upstream dca.conf visible to ALSA through one project-specific
# system include. Reusing this exact file also covers the earlier manual fix.
if [[ ! -e $ALSA_INCLUDE ]]; then
    printf '%s\n' '<confdir:pcm/dca.conf>' > "$ALSA_INCLUDE"
    chmod 644 "$ALSA_INCLUDE"
fi

# Old versions appended an ALSA include to only the installing user's file.
# Remove precisely that line when the old installer recorded ownership.
if [[ -f $PROJECT_STATE/asoundrc-include-added ]]; then
    python3 - "$TARGET_HOME/.asoundrc" <<'PY'
import os, pathlib, sys, tempfile
p = pathlib.Path(sys.argv[1])
if p.is_file():
    old = p.read_bytes()
    new = old.replace(b'\n<confdir:pcm/dca.conf>\n', b'', 1)
    if new == old and old == b'<confdir:pcm/dca.conf>\n':
        new = b''
    if new != old:
        if not new.strip():
            p.unlink()
        else:
            fd, name = tempfile.mkstemp(dir=p.parent)
            try:
                os.fchmod(fd, p.stat().st_mode & 0o777)
                os.fchown(fd, p.stat().st_uid, p.stat().st_gid)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(new)
                os.replace(name, p)
            finally:
                if os.path.exists(name): os.unlink(name)
PY
    rm -f "$PROJECT_STATE/asoundrc-include-added"
fi

# Remove only exact filenames created by earlier development builds. Parent
# directories are deliberately preserved because they may contain user files.
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi0.conf" \
      "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi1.conf" \
      "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-rpi-hdmi-ac3.conf" \
      "$TARGET_HOME/.config/pipewire/pipewire.conf.d/90-rpi-hdmi-audio-names.conf" \
      "$TARGET_HOME/.config/autostart/rpi-hdmi-audio.desktop" \
      "$TARGET_HOME/.config/autostart/rpi-hdmi-audio-restore.desktop"
rm -f /usr/local/bin/add-channel-volume.py /usr/local/bin/add-channel-volume-v2.py

STATE_DIR="$TARGET_HOME/.local/state/rpi-hdmi-audio"
for dir in "$TARGET_HOME/.local" "$TARGET_HOME/.local/state" "$STATE_DIR"; do
    if [[ ! -d $dir ]]; then
        install -d -o "$TARGET_UID" -g "$TARGET_GID" -m 700 "$dir"
    fi
done
STATE_FILE="$STATE_DIR/last-choice"

# Preserve the saved Volume/Frequency lines during upgrades. If there is no
# existing state yet, use hdmi0-pcm51 as the internal key for PCM (Default).
# In v16.5 that key means native PipeWire PCM, not a forced six-channel sink.
active_choice=
if session_live; then
    case $(user_cmd pactl get-default-sink 2>/dev/null || true) in
        rpi_hdmi_audio_hdmi0_stereo) active_choice=hdmi0-stereo ;;
        rpi_hdmi_audio_hdmi0_pcm51)  active_choice=hdmi0-pcm51 ;;
        rpi_hdmi_audio_hdmi0_ac3)    active_choice=hdmi0-ac3 ;;
        rpi_hdmi_audio_hdmi0_dts)    active_choice=hdmi0-dts ;;
        rpi_hdmi_audio_hdmi1_stereo) active_choice=hdmi1-stereo ;;
        rpi_hdmi_audio_hdmi1_pcm51)  active_choice=hdmi1-pcm51 ;;
        rpi_hdmi_audio_hdmi1_ac3)    active_choice=hdmi1-ac3 ;;
        rpi_hdmi_audio_hdmi1_dts)    active_choice=hdmi1-dts ;;
    esac
fi
if [[ -n $active_choice || ! -s $STATE_FILE ]]; then
    { printf '%s\n' "${active_choice:-hdmi0-pcm51}"
      [[ ! -f $STATE_FILE ]] || tail -n +2 "$STATE_FILE"
    } > "$STATE_FILE.install-tmp"
    mv -f "$STATE_FILE.install-tmp" "$STATE_FILE"
fi
chown "$TARGET_UID:$TARGET_GID" "$STATE_FILE"
chmod 600 "$STATE_FILE"

# ---------------------------------------------------------------------------
# Stop the background helpers from the previously installed version.
#
# The application already provides dedicated commands for its restore/volume
# monitor and its Dolby/DTS keepalive stream. Using those commands is safer
# and easier to understand than scanning all desktop processes manually.
# A fresh installation has no old binary yet, so this block simply does nothing.
# ---------------------------------------------------------------------------
if [[ -x $APP ]]; then
    user_cmd "$APP" --stop-monitor >/dev/null 2>&1 || true
    user_cmd "$APP" --stop-keepalive >/dev/null 2>&1 || true
fi

# Install the executable and let the application generate its own PipeWire
# filter-chain fragment for per-speaker Volume and Frequency Balance.
install -m 755 "$APP_SOURCE" "$APP"
user_cmd "$APP" --write-filter-config

# Install the visible GNOME application launcher.
cat > /usr/share/applications/rpi-hdmi-audio.desktop <<'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=HDMI Audio Encoder
Comment=Choose PCM Default, Stereo 2.0, Dolby Digital or DTS with Frequency Balance
Exec=/usr/local/bin/rpi-hdmi-audio
Icon=audio-card
Terminal=false
Categories=Settings;AudioVideo;Audio;
Keywords=audio;hdmi;pcm;dolby;ac3;dts;surround;frequency;raspberry;
DESKTOP
chmod 644 /usr/share/applications/rpi-hdmi-audio.desktop

# Install one hidden GNOME autostart entry. It does not open the GUI; it
# restores the saved output and then monitors external GNOME volume changes.
cat > /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=HDMI Audio Encoder Restore
Comment=Restore the last HDMI audio mode after login
Exec=/usr/local/bin/rpi-hdmi-audio --restore-monitor
Icon=audio-card
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
DESKTOP
chmod 644 /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop
update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

# Reload the user audio services so the new filter configuration is visible.
# Then start the single restore/volume-monitor process in the desktop session.
if [[ -S /run/user/$TARGET_UID/bus ]]; then
    user_cmd systemctl --user restart pipewire pipewire-pulse wireplumber
    for _ in {1..40}; do
        user_cmd pactl info >/dev/null 2>&1 && break
        sleep 0.2
    done
    if session_live; then
        # This one process performs both restore and volume monitoring.
        runuser -u "$TARGET_USER" -- env \
            HOME="$TARGET_HOME" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
            nohup "$APP" --restore-monitor </dev/null >/dev/null 2>&1 &
    else
        echo 'Audio is not reachable yet; log in again to restore the saved output.' >&2
    fi
fi

printf 'Complete: version 16.5. PCM (Default) is the initial mode on a fresh installation.\n'
