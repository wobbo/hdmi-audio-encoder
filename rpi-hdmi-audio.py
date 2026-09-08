#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk


# ===========================================================================
# APPLICATION SETTINGS
#
# General names and timing values used by the application.
#
# SWITCH_LOCK_SECONDS:
#     Prevents another click while a switch is still starting.
#
# AC3_PRIME_SECONDS:
#     Sends silent six-channel audio through the A52 encoder before and
#     during the moment that real application audio is moved to Dolby.
#
# AC3_PRIME_LEAD_SECONDS:
#     How long Dolby runs before real application audio is moved to it.
# ===========================================================================

APP_ID = "org.wobbo.RPiHDMIAudio"

# The last successful user selection is stored as application state.
# This is not a PipeWire or WirePlumber configuration file. It only lets the
# headless login restore rebuild the same direct HDMI sink after a reboot.
STATE_DIR = Path.home() / ".local" / "state" / "rpi-hdmi-audio"
STATE_FILE = STATE_DIR / "last-choice"

SWITCH_LOCK_SECONDS = 2.0

AC3_PRIME_SECONDS = 1.5
AC3_PRIME_LEAD_SECONDS = 0.40

RELEASE_TIMEOUT_SECONDS = 4.0
SINK_TIMEOUT_SECONDS = 3.0

LOAD_RETRIES = 5
LOAD_RETRY_DELAY_SECONDS = 0.30


# ===========================================================================
# RASPBERRY PI HDMI CARDS
#
# These are the stable PipeWire card names for HDMI 0 and HDMI 1 on this
# Raspberry Pi.
#
# The normal PipeWire HDMI profiles must be disabled before module-alsa-sink
# can open the same physical HDMI device directly through ALSA.
# ===========================================================================

CARD0 = "alsa_card.platform-107c701400.hdmi"
CARD1 = "alsa_card.platform-107c706400.hdmi"

PROFILE_STEREO = "output:hdmi-stereo"
PROFILE_AC3 = "output:hdmi-ac3"

MODE_STEREO = "stereo"
MODE_AC3 = "ac3"


# ===========================================================================
# TEMPORARY HOLDING SINK
#
# A running application such as YouTube keeps a live PipeWire audio stream.
#
# Removing the HDMI sink while that stream is still attached to it can cause
# PipeWire and the browser to reroute audio at the same moment that ALSA is
# trying to release or reopen HDMI.
#
# To avoid that race:
#
#     YouTube / game / media player
#                 |
#                 v
#        temporary holding sink
#                 |
#        HDMI is safely changed
#                 |
#                 v
#        new real HDMI output
#
# The holding sink has no hardware. Audio sent to it is silently discarded
# for the short duration of the switch. Its channel layout follows the target
# mode, so restarting Stereo stays visually 2.0 in GNOME instead of briefly
# appearing as 5.1.
# ===========================================================================

HOLDING_SINK = "rpi_hdmi_audio_holding"

HOLDING_DESCRIPTION = "HDMI"


# ===========================================================================
# AUDIO OUTPUT DEFINITIONS
#
# These are the four choices shown in the application.
#
# Stereo opens the normal HDMI PCM device.
#
# Dolby Digital opens the ALSA A52 plugin:
#
#     application PCM 5.1
#          -> PipeWire
#          -> module-alsa-sink
#          -> ALSA A52 encoder
#          -> AC-3 / Dolby Digital
#          -> HDMI
#          -> HDMI audio extractor
#          -> TOSLINK
#          -> Sony receiver
#
# The simple A52 device strings below are the same method that worked in the
# successful manual module-alsa-sink test.
# ===========================================================================


@dataclass(frozen=True)
class Choice:
    key: str
    title: str
    subtitle: str

    card: str
    other_card: str

    mode: str
    profile: str

    alsa_device: str

    sink_name: str
    description: str

    channels: int
    channel_map: str


CHOICES = (
    Choice(
        key="hdmi0-stereo",
        title="HDMI 0 · Stereo 2.0",
        subtitle="PCM stereo for monitor / normal HDMI audio",

        card=CARD0,
        other_card=CARD1,

        mode=MODE_STEREO,
        profile=PROFILE_STEREO,

        alsa_device="hdmi:CARD=vc4hdmi0,DEV=0",

        sink_name="rpi_hdmi_audio_hdmi0_stereo",
        description="HDMI 0 - Stereo 2.0",

        channels=2,
        channel_map="front-left,front-right",
    ),

    Choice(
        key="hdmi0-ac3",
        title="HDMI 0 · Dolby Digital 5.1",
        subtitle="Realtime AC-3 5.1 for HDMI → TOSLINK → receiver",

        card=CARD0,
        other_card=CARD1,

        mode=MODE_AC3,
        profile=PROFILE_AC3,

        alsa_device=(
            "plug:{SLAVE="
            "\"a52:0,'hdmi:CARD=vc4hdmi0,DEV=0'\"}"
        ),

        sink_name="rpi_hdmi_audio_hdmi0_ac3",
        description="HDMI 0 - Dolby Digital 5.1",

        channels=6,
        channel_map=(
            "front-left,"
            "front-right,"
            "rear-left,"
            "rear-right,"
            "front-center,"
            "lfe"
        ),
    ),

    Choice(
        key="hdmi1-stereo",
        title="HDMI 1 · Stereo 2.0",
        subtitle="PCM stereo for monitor / normal HDMI audio",

        card=CARD1,
        other_card=CARD0,

        mode=MODE_STEREO,
        profile=PROFILE_STEREO,

        alsa_device="hdmi:CARD=vc4hdmi1,DEV=0",

        sink_name="rpi_hdmi_audio_hdmi1_stereo",
        description="HDMI 1 - Stereo 2.0",

        channels=2,
        channel_map="front-left,front-right",
    ),

    Choice(
        key="hdmi1-ac3",
        title="HDMI 1 · Dolby Digital 5.1",
        subtitle="Realtime AC-3 5.1 for HDMI → TOSLINK → receiver",

        card=CARD1,
        other_card=CARD0,

        mode=MODE_AC3,
        profile=PROFILE_AC3,

        alsa_device=(
            "plug:{SLAVE="
            "\"a52:1,'hdmi:CARD=vc4hdmi1,DEV=0'\"}"
        ),

        sink_name="rpi_hdmi_audio_hdmi1_ac3",
        description="HDMI 1 - Dolby Digital 5.1",

        channels=6,
        channel_map=(
            "front-left,"
            "front-right,"
            "rear-left,"
            "rear-right,"
            "front-center,"
            "lfe"
        ),
    ),
)


CHOICE_BY_KEY = {
    choice.key: choice
    for choice in CHOICES
}

CHOICE_BY_SINK = {
    choice.sink_name: choice
    for choice in CHOICES
}


# ===========================================================================
# SAVED USER SELECTION
#
# module-alsa-sink modules disappear when PipeWire or the computer restarts.
# The application therefore stores only the key of the last successfully
# selected output, for example:
#
#     hdmi0-stereo
#     hdmi0-ac3
#     hdmi1-stereo
#     hdmi1-ac3
#
# At the next GNOME login the hidden --restore mode reads this tiny state file
# and rebuilds the same direct sink without opening the application window.
# ===========================================================================


def save_choice_state(choice: Choice) -> None:

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = STATE_FILE.with_suffix(
        ".tmp"
    )

    temporary.write_text(
        choice.key + "\n",
        encoding="utf-8",
    )

    temporary.replace(
        STATE_FILE
    )


def load_choice_state() -> Choice | None:

    try:
        key = (
            STATE_FILE
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, OSError):
        return None

    return CHOICE_BY_KEY.get(
        key
    )


# ===========================================================================
# OLD DEVELOPMENT / TEST SINKS
#
# pactl modules continue to exist inside PipeWire even when the Python
# application that created them has been closed.
#
# These older names are therefore recognised and removed when changing
# output. This includes the manual rpi_test_ac3 test sink.
# ===========================================================================

LEGACY_SINKS = {
    "rpi_test_ac3": "hdmi0-ac3",

    "hdmi0_ac3": "hdmi0-ac3",
    "hdmi1_ac3": "hdmi1-ac3",

    "hdmi0_stereo": "hdmi0-stereo",
    "hdmi1_stereo": "hdmi1-stereo",
}


OUTPUT_SINK_NAMES = (
    set(CHOICE_BY_SINK)
    | set(LEGACY_SINKS)
)


# ===========================================================================
# PACTL HELPER
#
# Runs PipeWire's PulseAudio-compatible control command and captures both
# stdout and stderr so actual failures can be shown in the application.
# ===========================================================================


def run(
    cmd: list[str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:

    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


# ===========================================================================
# PULSE MODULE SINK PROPERTIES
#
# PipeWire's PulseAudio module parser expects the complete property list to be
# quoted when a human-readable description contains spaces. This exact form
# was verified manually on the Raspberry Pi:
#
#     sink_properties='device.description="HDMI 0 - Test Name"'
#
# Dolby Digital needs one additional property. During an automatic login
# restore there may be no application audio yet. WirePlumber normally suspends
# an idle ALSA sink after a few seconds. The A52/AC-3 path on this Raspberry Pi
# can fail to recover from that suspend, which is why Dolby may look selected
# after a reboot but remain silent until it is restarted manually.
#
# For Dolby only, session.suspend-timeout-seconds=0 keeps the ALSA A52 sink
# open while it is selected. Stereo keeps the normal power-saving behaviour.
# This is carried by the sink itself, so no extra WirePlumber configuration
# file is required.
# ===========================================================================


def sink_properties_argument(
    description: str,
    keep_open: bool = False,
) -> str:

    escaped = (
        description
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    properties = [
        f'device.description="{escaped}"'
    ]

    if keep_open:
        properties.extend(
            [
                "session.suspend-timeout-seconds=0",
                "node.suspend-on-idle=false",
            ]
        )

    return (
        "sink_properties='"
        + " ".join(properties)
        + "'"
    )


# ===========================================================================
# READ CURRENT PIPEWIRE STATE
# ===========================================================================


def get_sinks() -> dict[str, str]:
    """
    Return:

        sink name -> sink numeric index
    """

    result = run(
        [
            "pactl",
            "list",
            "sinks",
            "short",
        ],
        check=False,
    )

    sinks: dict[str, str] = {}

    for line in result.stdout.splitlines():

        fields = line.split("\t")

        if len(fields) >= 2:

            sink_index = fields[0].strip()
            sink_name = fields[1].strip()

            sinks[sink_name] = sink_index

    return sinks


def get_sink_inputs() -> dict[str, str]:
    """
    Return:

        sink-input ID -> sink numeric index

    A sink-input is a running application audio stream such as YouTube.
    """

    result = run(
        [
            "pactl",
            "list",
            "sink-inputs",
            "short",
        ],
        check=False,
    )

    inputs: dict[str, str] = {}

    for line in result.stdout.splitlines():

        fields = line.split("\t")

        if len(fields) >= 2:

            input_id = fields[0].strip()
            sink_index = fields[1].strip()

            if input_id.isdigit():
                inputs[input_id] = sink_index

    return inputs


def get_module_lines() -> list[str]:

    result = run(
        [
            "pactl",
            "list",
            "modules",
            "short",
        ],
        check=False,
    )

    return result.stdout.splitlines()


def get_card_profiles() -> dict[str, str]:

    result = run(
        [
            "pactl",
            "list",
            "cards",
        ],
        check=False,
    )

    profiles: dict[str, str] = {}

    current_name: str | None = None

    for raw in result.stdout.splitlines():

        line = raw.strip()

        if line.startswith("Name: "):

            current_name = (
                line
                .removeprefix("Name: ")
                .strip()
            )

        elif (
            line.startswith("Active Profile: ")
            and current_name
        ):

            profiles[current_name] = (
                line
                .removeprefix("Active Profile: ")
                .strip()
            )

            current_name = None

    return profiles


# ===========================================================================
# DETECT CURRENT SELECTION
#
# New versions use our direct sink names.
#
# Older versions used custom PipeWire card profiles, so those are still
# recognised as a fallback.
# ===========================================================================


def get_current_choice() -> Choice | None:

    sinks = get_sinks()

    for sink_name, choice in CHOICE_BY_SINK.items():

        if sink_name in sinks:
            return choice

    for sink_name, choice_key in LEGACY_SINKS.items():

        if sink_name in sinks:
            return CHOICE_BY_KEY[choice_key]

    profiles = get_card_profiles()

    for choice in CHOICES:

        if (
            profiles.get(choice.card)
            == choice.profile
            and profiles.get(
                choice.other_card,
                "off",
            )
            == "off"
        ):
            return choice

    return None


# ===========================================================================
# FIND MODULES BY THEIR SINK NAME
#
# This only selects modules created by this project.
#
# Unrelated PipeWire devices are left alone.
# ===========================================================================


def get_module_ids_for_sink_names(
    sink_names: set[str],
) -> list[str]:

    module_ids: list[str] = []

    for line in get_module_lines():

        if (
            "module-alsa-sink" not in line
            and "module-null-sink" not in line
        ):
            continue

        found = any(
            f"sink_name={sink_name}" in line
            for sink_name in sink_names
        )

        if not found:
            continue

        fields = line.split(
            "\t",
            1,
        )

        if (
            fields
            and fields[0].strip().isdigit()
        ):
            module_ids.append(
                fields[0].strip()
            )

    return module_ids


# ===========================================================================
# TEMPORARY HOLDING SINK
#
# The holding sink is created BEFORE HDMI is touched. Its channel count and
# channel map follow the target output choice.
#
# The system default is changed to the holding sink first. Existing
# application streams are then explicitly moved there.
#
# This means:
#
#     - existing YouTube audio remains connected
#     - a browser stream recreated during the switch also goes to holding
#     - the physical HDMI device can be released without a live application
#       stream fighting for it
# ===========================================================================


def load_holding_sink(choice: Choice) -> None:

    sinks = get_sinks()

    if HOLDING_SINK in sinks:
        return

    result = run(
        [
            "pactl",
            "load-module",
            "module-null-sink",

            f"sink_name={HOLDING_SINK}",

            "rate=48000",

            f"channels={choice.channels}",

            f"channel_map={choice.channel_map}",

            sink_properties_argument(
                HOLDING_DESCRIPTION
            ),
        ],
        check=False,
    )

    module_id = result.stdout.strip()

    if (
        result.returncode != 0
        or not module_id.isdigit()
    ):

        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Unknown PipeWire error."
        )

        raise RuntimeError(
            "Could not create the temporary holding sink: "
            + detail
        )

    if not wait_for_sink(
        HOLDING_SINK,
        SINK_TIMEOUT_SECONDS,
    ):

        raise RuntimeError(
            "The temporary holding sink did not appear in time."
        )


def set_default_sink(
    sink_name: str,
) -> None:

    run(
        [
            "pactl",
            "set-default-sink",
            sink_name,
        ]
    )


def move_all_streams_to_sink(
    sink_name: str,
) -> None:

    sinks = get_sinks()

    target_index = sinks.get(
        sink_name
    )

    if target_index is None:

        raise RuntimeError(
            f"Audio sink {sink_name} does not exist."
        )

    inputs = get_sink_inputs()

    for input_id, current_sink_index in inputs.items():

        if current_sink_index == target_index:
            continue

        run(
            [
                "pactl",
                "move-sink-input",
                input_id,
                sink_name,
            ],
            check=False,
        )


def park_all_application_audio(choice: Choice) -> None:

    load_holding_sink(choice)

    # Any NEW application stream created during the switch will now also
    # automatically start on the holding sink.

    set_default_sink(
        HOLDING_SINK
    )

    # Move the streams that already existed.

    move_all_streams_to_sink(
        HOLDING_SINK
    )

    # Run a second pass because browsers may recreate a stream while the first
    # move is taking place.

    time.sleep(0.10)

    move_all_streams_to_sink(
        HOLDING_SINK
    )

    time.sleep(0.10)


# ===========================================================================
# REMOVE OLD REAL HDMI OUTPUTS
#
# The holding sink is deliberately NOT removed here.
#
# Application audio remains safely parked while the physical HDMI device is
# closed and reopened.
# ===========================================================================


def unload_output_modules() -> None:

    module_ids = (
        get_module_ids_for_sink_names(
            OUTPUT_SINK_NAMES
        )
    )

    for module_id in module_ids:

        run(
            [
                "pactl",
                "unload-module",
                module_id,
            ],
            check=False,
        )


def release_normal_hdmi_cards() -> None:

    run(
        [
            "pactl",
            "set-card-profile",
            CARD0,
            "off",
        ],
        check=False,
    )

    run(
        [
            "pactl",
            "set-card-profile",
            CARD1,
            "off",
        ],
        check=False,
    )


def output_modules_are_gone() -> bool:

    return not get_module_ids_for_sink_names(
        OUTPUT_SINK_NAMES
    )


def output_sinks_are_gone() -> bool:

    sinks = get_sinks()

    if set(sinks) & OUTPUT_SINK_NAMES:
        return False

    return True


def normal_hdmi_sinks_are_gone() -> bool:

    for sink_name in get_sinks():

        if "107c701400" in sink_name:
            return False

        if "107c706400" in sink_name:
            return False

    return True


def wait_until_hdmi_is_free() -> None:

    deadline = (
        time.monotonic()
        + RELEASE_TIMEOUT_SECONDS
    )

    while time.monotonic() < deadline:

        if (
            output_modules_are_gone()
            and output_sinks_are_gone()
            and normal_hdmi_sinks_are_gone()
        ):

            # PipeWire objects are now gone. ALSA can still require a very
            # short moment to finish closing the hardware internally.

            time.sleep(0.20)

            return

        time.sleep(0.05)

    raise RuntimeError(
        "The previous HDMI output did not release completely in time."
    )


def fully_release_real_hdmi() -> None:

    unload_output_modules()

    release_normal_hdmi_cards()

    wait_until_hdmi_is_free()


# ===========================================================================
# WAIT FOR A SINK
#
# pactl load-module can return slightly before the new PipeWire sink itself
# appears. This function waits for that actual sink object.
# ===========================================================================


def wait_for_sink(
    sink_name: str,
    timeout_seconds: float,
) -> bool:

    deadline = (
        time.monotonic()
        + timeout_seconds
    )

    while time.monotonic() < deadline:

        if sink_name in get_sinks():
            return True

        time.sleep(0.05)

    return False


# ===========================================================================
# CREATE THE REAL STEREO OR DOLBY SINK
#
# We use module-alsa-sink for both.
#
# The difference is the ALSA device:
#
# Stereo:
#     hdmi:CARD=...
#
# Dolby:
#     plug -> a52 -> hdmi:CARD=...
#
# A human-readable device.description is supplied so GNOME Settings shows:
#
#     HDMI 0 - Stereo 2.0
#
# instead of:
#
#     ALSA Sink on plug:{SLAVE=...
# ===========================================================================


def load_choice_sink(
    choice: Choice,
) -> None:

    last_error = "Unknown error."

    for attempt in range(
        1,
        LOAD_RETRIES + 1,
    ):

        result = run(
            [
                "pactl",
                "load-module",
                "module-alsa-sink",

                f"device={choice.alsa_device}",

                f"sink_name={choice.sink_name}",

                "rate=48000",

                f"channels={choice.channels}",

                f"channel_map={choice.channel_map}",

                "tsched=0",

                sink_properties_argument(
                    choice.description,
                    keep_open=(choice.mode == MODE_AC3),
                ),
            ],
            check=False,
        )

        module_id = result.stdout.strip()

        if (
            result.returncode == 0
            and module_id.isdigit()
        ):

            if wait_for_sink(
                choice.sink_name,
                SINK_TIMEOUT_SECONDS,
            ):

                return

            last_error = (
                "The module was created, "
                "but its audio sink did not appear."
            )

            run(
                [
                    "pactl",
                    "unload-module",
                    module_id,
                ],
                check=False,
            )

        else:

            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or (
                    f"Attempt {attempt} failed "
                    "without an error message."
                )
            )

        # A failed ALSA open may leave the HDMI device busy for a fraction
        # of a second. Clean it again before the next attempt.

        unload_output_modules()

        release_normal_hdmi_cards()

        try:
            wait_until_hdmi_is_free()

        except RuntimeError:
            pass

        time.sleep(
            LOAD_RETRY_DELAY_SECONDS
        )

    raise RuntimeError(
        f"Could not open {choice.title} after "
        f"{LOAD_RETRIES} attempts: {last_error}"
    )


# ===========================================================================
# DOLBY DIGITAL PRIMING
#
# The A52 sink does not output meaningful Dolby frames until PCM audio reaches
# it.
#
# Before YouTube is moved from the holding sink, this function starts a short
# stream of silent six-channel PCM.
#
# ALSA encodes that silence as real AC-3.
#
# The Sony therefore gets a chance to recognise Dolby Digital BEFORE the
# browser audio arrives.
#
# The silent stream remains active for a short period AFTER real application
# audio is moved, avoiding a gap between "Dolby startup" and real sound.
# ===========================================================================


def send_ac3_silence(
    sink_name: str,
    seconds: float,
) -> None:

    if shutil.which("paplay") is None:
        return

    sample_rate = 48000
    channels = 6
    bytes_per_sample = 2

    frames = int(
        sample_rate * seconds
    )

    silence = b"\x00" * (
        frames
        * channels
        * bytes_per_sample
    )

    subprocess.run(
        [
            "paplay",
            "--raw",

            f"--device={sink_name}",

            "--rate=48000",

            "--channels=6",

            "--format=s16le",

            (
                "--channel-map="
                "front-left,"
                "front-right,"
                "rear-left,"
                "rear-right,"
                "front-center,"
                "lfe"
            ),
        ],
        input=silence,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def start_ac3_prime(
    sink_name: str,
) -> threading.Thread:

    thread = threading.Thread(
        target=send_ac3_silence,
        args=(
            sink_name,
            AC3_PRIME_SECONDS,
        ),
        daemon=True,
    )

    thread.start()

    return thread


# ===========================================================================
# MOVE APPLICATION AUDIO FROM HOLDING TO THE REAL OUTPUT
#
# The real output becomes the default FIRST.
#
# Therefore, if Firefox / Chromium creates a fresh stream during this moment,
# the new stream already goes to the real HDMI output instead of back to the
# temporary holding sink.
# ===========================================================================


def drain_holding_sink(
    target_sink: str,
) -> None:

    set_default_sink(
        target_sink
    )

    deadline = (
        time.monotonic()
        + 2.0
    )

    while time.monotonic() < deadline:

        sinks = get_sinks()

        holding_index = sinks.get(
            HOLDING_SINK
        )

        if holding_index is None:
            return

        inputs = get_sink_inputs()

        holding_inputs = [
            input_id
            for input_id, sink_index
            in inputs.items()
            if sink_index == holding_index
        ]

        if not holding_inputs:
            return

        for input_id in holding_inputs:

            run(
                [
                    "pactl",
                    "move-sink-input",
                    input_id,
                    target_sink,
                ],
                check=False,
            )

        time.sleep(0.05)

    raise RuntimeError(
        "One or more application audio streams "
        "could not leave the temporary holding sink."
    )


# ===========================================================================
# REMOVE THE HOLDING SINK
#
# This happens only AFTER all application streams have reached the new real
# HDMI output.
# ===========================================================================


def remove_holding_sink() -> None:

    module_ids = (
        get_module_ids_for_sink_names(
            {HOLDING_SINK}
        )
    )

    for module_id in module_ids:

        run(
            [
                "pactl",
                "unload-module",
                module_id,
            ],
            check=False,
        )


# ===========================================================================
# COMPLETE AUDIO SWITCH
#
# The important sequence is:
#
#     1. Create holding sink
#     2. Move YouTube / other running audio to holding
#     3. Completely release old HDMI
#     4. Open new Stereo or Dolby HDMI
#     5. For Dolby, start valid AC-3 silence
#     6. Move YouTube to the new HDMI sink
#     7. Remove holding sink
#
# YouTube is never paused.
#
# Its existing PipeWire audio stream remains alive during the complete switch.
# ===========================================================================


def activate_choice(
    choice: Choice,
) -> None:

    park_all_application_audio(
        choice
    )

    fully_release_real_hdmi()

    load_choice_sink(
        choice
    )

    prime_thread: threading.Thread | None = None

    if choice.mode == MODE_AC3:

        prime_thread = start_ac3_prime(
            choice.sink_name
        )

        # Give the Sony a short head start on the AC-3 stream.

        time.sleep(
            AC3_PRIME_LEAD_SECONDS
        )

    drain_holding_sink(
        choice.sink_name
    )

    # Let the prime stream overlap the real audio.
    #
    # This avoids:
    #
    #     AC-3 silence stops
    #     -> digital gap
    #     -> real audio starts
    #
    # which could cause the receiver to lose lock again.

    if prime_thread is not None:

        prime_thread.join(
            timeout=(
                AC3_PRIME_SECONDS
                + 1.0
            )
        )

    time.sleep(0.10)

    remove_holding_sink()


# ===========================================================================
# APPLY SELECTION WITH ROLLBACK
#
# If the requested mode cannot be opened after all retries, the previous
# selection is restored.
#
# The holding sink remains available while this happens, so running browser
# audio does not need to be destroyed or recreated.
# ===========================================================================


def apply_choice(
    choice: Choice,
) -> str:

    previous = get_current_choice()

    try:

        activate_choice(
            choice
        )

        # Persist only a selection that actually became active. If switching
        # fails and rollback is needed, the previous saved choice is kept.
        save_choice_state(
            choice
        )

        return (
            f"Active: {choice.title}"
        )

    except Exception as original_error:

        # Keep or recreate holding so application audio has somewhere safe
        # to remain while trying to restore the old output.

        try:
            park_all_application_audio(
                previous or choice
            )
        except Exception:
            pass

        try:
            fully_release_real_hdmi()
        except Exception:
            pass

        if previous is not None:

            try:

                load_choice_sink(
                    previous
                )

                set_default_sink(
                    previous.sink_name
                )

                drain_holding_sink(
                    previous.sink_name
                )

                remove_holding_sink()

            except Exception:
                pass

        raise RuntimeError(
            str(original_error)
        ) from original_error


# ===========================================================================
# GTK4 / LIBADWAITA INTERFACE
#
# The radio buttons are only visual indicators.
#
# The complete rows are clickable so clicking an already-selected row can
# deliberately restart that same HDMI output.
# ===========================================================================


class AudioWindow(
    Adw.ApplicationWindow
):

    def __init__(
        self,
        app: Adw.Application,
    ):

        super().__init__(
            application=app
        )

        self.set_title(
            "RPi HDMI Audio"
        )

        self.set_icon_name(
            "audio-card"
        )

        self.set_default_size(
            520,
            390,
        )

        self.switch_locked = False
        self.switch_started_at = 0.0

        toolbar = Adw.ToolbarView()

        self.set_content(
            toolbar
        )

        header = Adw.HeaderBar()

        toolbar.add_top_bar(
            header
        )

        page = Adw.PreferencesPage()

        toolbar.set_content(
            page
        )

        group = Adw.PreferencesGroup(
            title="Audio output",
            description=(
                "Choose one HDMI port "
                "and one audio mode."
            ),
        )

        page.add(
            group
        )

        self.status = Adw.ActionRow(
            title="Status",
            subtitle="Reading audio settings…",
        )

        self.status.add_prefix(
            Gtk.Image.new_from_icon_name(
                "audio-card-symbolic"
            )
        )

        group.add(
            self.status
        )

        self.rows: dict[
            str,
            tuple[
                Adw.ActionRow,
                Gtk.CheckButton,
            ],
        ] = {}

        first_button: (
            Gtk.CheckButton | None
        ) = None


        # -------------------------------------------------------------------
        # CREATE THE FOUR AUDIO ROWS
        # -------------------------------------------------------------------

        for choice in CHOICES:

            row = Adw.ActionRow(
                title=choice.title,
                subtitle=choice.subtitle,
            )

            row.set_activatable(
                True
            )

            button = Gtk.CheckButton()

            button.set_valign(
                Gtk.Align.CENTER
            )

            # The small radio button does not receive the click itself.
            # Clicking anywhere on the complete row starts the switch.

            button.set_can_focus(
                False
            )

            button.set_can_target(
                False
            )

            if first_button is None:

                first_button = button

            else:

                button.set_group(
                    first_button
                )

            row.add_suffix(
                button
            )

            row.connect(
                "activated",
                self.on_row_activated,
                choice,
            )

            group.add(
                row
            )

            self.rows[
                choice.key
            ] = (
                row,
                button,
            )


        # -------------------------------------------------------------------
        # DOLBY INFORMATION
        #
        # The application deliberately does not change monitor volume.
        # -------------------------------------------------------------------

        note = Adw.PreferencesGroup(
            title="Important"
        )

        page.add(
            note
        )

        info = Adw.ActionRow(
            title="Dolby Digital 5.1",
            subtitle=(
                "A monitor without an AC-3 decoder "
                "may produce loud digital noise. "
                "This application does not change "
                "monitor volume settings."
            ),
        )

        info.add_prefix(
            Gtk.Image.new_from_icon_name(
                "dialog-information-symbolic"
            )
        )

        note.add(
            info
        )

        GLib.idle_add(
            self.refresh
        )


    # =======================================================================
    # ENABLE / DISABLE ROWS
    # =======================================================================

    def set_rows_sensitive(
        self,
        sensitive: bool,
    ) -> None:

        for row, _ in self.rows.values():

            row.set_sensitive(
                sensitive
            )


    # =======================================================================
    # UPDATE RADIO BUTTON IMMEDIATELY
    #
    # This happens as soon as a row is clicked.
    #
    # It does not wait for the slower PipeWire / ALSA switch.
    # =======================================================================

    def set_visual_choice(
        self,
        choice: Choice,
    ) -> None:

        _, button = (
            self.rows[
                choice.key
            ]
        )

        button.set_active(
            True
        )


    # =======================================================================
    # REFRESH FROM REAL PIPEWIRE STATE
    # =======================================================================

    def refresh(
        self,
    ) -> bool:

        try:

            current = (
                get_current_choice()
            )

            if current is None:

                self.status.set_subtitle(
                    "No active RPi HDMI Audio output found."
                )

                return False

            self.status.set_subtitle(
                f"Active: {current.title}"
            )

            self.set_visual_choice(
                current
            )

        except Exception as exc:

            self.status.set_subtitle(
                f"Error: {exc}"
            )

        return False


    # =======================================================================
    # USER CLICK
    #
    # Selecting the already-active row is allowed.
    #
    # That performs a complete reconnect, which is useful if the receiver
    # ever fails to lock onto Dolby Digital.
    # =======================================================================

    def on_row_activated(
        self,
        row: Adw.ActionRow,
        choice: Choice,
    ) -> None:

        if self.switch_locked:
            return

        current = get_current_choice()

        restarting = (
            current is not None
            and current.key == choice.key
        )

        # Change the visual selection immediately.

        self.set_visual_choice(
            choice
        )

        self.switch_locked = True

        self.switch_started_at = (
            time.monotonic()
        )

        self.set_rows_sensitive(
            False
        )

        if restarting:

            self.status.set_subtitle(
                f"Restarting {choice.title}…"
            )

        else:

            self.status.set_subtitle(
                f"Switching to {choice.title}…"
            )


        def worker() -> None:

            try:

                message = apply_choice(
                    choice
                )

                GLib.idle_add(
                    self.switch_done,
                    message,
                    None,
                )

            except Exception as exc:

                GLib.idle_add(
                    self.switch_done,
                    None,
                    str(exc),
                )


        threading.Thread(
            target=worker,
            daemon=True,
        ).start()


    # =======================================================================
    # SWITCH FINISHED
    #
    # The rows stay disabled for at least two seconds from the original click.
    # =======================================================================

    def switch_done(
        self,
        message: str | None,
        error: str | None,
    ) -> bool:

        if error:

            self.status.set_subtitle(
                f"Error: {error}"
            )

        else:

            self.status.set_subtitle(
                message
                or "Audio output changed."
            )

        self.refresh()

        elapsed = (
            time.monotonic()
            - self.switch_started_at
        )

        remaining = max(
            0.0,
            SWITCH_LOCK_SECONDS
            - elapsed,
        )

        if remaining <= 0:

            self.unlock_switching()

        else:

            GLib.timeout_add(
                max(
                    1,
                    int(
                        remaining
                        * 1000
                    ),
                ),
                self.unlock_switching,
            )

        return False


    # =======================================================================
    # ALLOW THE NEXT SWITCH
    # =======================================================================

    def unlock_switching(
        self,
    ) -> bool:

        self.switch_locked = False

        self.set_rows_sensitive(
            True
        )

        self.refresh()

        return False


# ===========================================================================
# HEADLESS LOGIN / REBOOT RESTORE
#
# Direct module-alsa-sink outputs are intentionally temporary. They disappear
# when PipeWire or the computer restarts, while the physical HDMI cards may
# still be remembered as "off".
#
# The installer starts this same program once at GNOME login with --restore.
# No application window is opened. The restore mode waits for PipeWire and the
# required Raspberry Pi HDMI card, reads the last successful user selection,
# and rebuilds that exact Stereo or Dolby sink.
#
# If no saved selection exists yet, HDMI 0 Stereo 2.0 is used. If rebuilding
# the saved direct sink fails, normal GNOME HDMI 0 Stereo is restored as a safe
# fallback so the desktop does not end up with only Dummy Output.
# ===========================================================================


def wait_for_audio_stack(
    required_card: str,
    timeout_seconds: float = 15.0,
) -> bool:

    deadline = (
        time.monotonic()
        + timeout_seconds
    )

    while time.monotonic() < deadline:

        if run(
            ["pactl", "info"],
            check=False,
        ).returncode == 0:

            cards = run(
                [
                    "pactl",
                    "list",
                    "cards",
                    "short",
                ],
                check=False,
            ).stdout

            if required_card in cards:
                return True

        time.sleep(0.20)

    return False


def restore_normal_hdmi0_stereo() -> int:

    # Remove only modules belonging to this application. This also clears a
    # partial holding or direct sink left by a failed restore attempt.
    try:
        unload_output_modules()
        remove_holding_sink()
    except Exception:
        pass

    result = run(
        [
            "pactl",
            "set-card-profile",
            CARD0,
            PROFILE_STEREO,
        ],
        check=False,
    )

    return 0 if result.returncode == 0 else 1


def restore_saved_choice() -> int:

    choice = (
        load_choice_state()
        or CHOICE_BY_KEY["hdmi0-stereo"]
    )

    if not wait_for_audio_stack(
        choice.card
    ):
        return 1

    try:
        activate_choice(
            choice
        )
        return 0

    except Exception:
        return restore_normal_hdmi0_stereo()


# ===========================================================================
# APPLICATION STARTUP
# ===========================================================================


class AudioApp(
    Adw.Application
):

    def __init__(
        self,
    ):

        super().__init__(
            application_id=APP_ID,
            flags=(
                Gio.ApplicationFlags.DEFAULT_FLAGS
            ),
        )


    def do_activate(
        self,
    ) -> None:

        win = self.props.active_window

        if win is None:

            win = AudioWindow(
                self
            )

        win.present()


def main() -> int:

    if "--restore" in sys.argv[1:]:
        return restore_saved_choice()

    app = AudioApp()

    return app.run(
        None
    )


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
