#!/usr/bin/env python3
from __future__ import annotations

import json
import fcntl
import math
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk


APP_ID = "org.wobbo.RPiHDMIAudio"
# Update both values for every released change to this script.
APP_VERSION = "16.4"
APP_VERSION_DATE = "2026-09-26 09:21"
DSP_CONFIG_REVISION = "16.4-herzien"
STATE_DIR = Path.home() / ".local" / "state" / "rpi-hdmi-audio"
STATE_FILE = STATE_DIR / "last-choice"
OPERATION_LOCK_FILE = STATE_DIR / "audio-operation.lock"
_operation_thread_lock = threading.RLock()
_operation_depth = threading.local()

SWITCH_LOCK_SECONDS = 2.0
KEEPALIVE_LEAD_SECONDS = 0.90
RECEIVER_RELEASE_SECONDS = 0.80
RELEASE_TIMEOUT_SECONDS = 4.0
SINK_TIMEOUT_SECONDS = 3.0
LOAD_RETRIES = 5
LOAD_RETRY_DELAY_SECONDS = 0.30
POST_SWITCH_WAKE_RETRIES = 3
POST_SWITCH_WAKE_DELAY_SECONDS = 0.16
POST_SWITCH_NUDGE_HOLD_SECONDS = 0.05

CARD0 = "alsa_card.platform-107c701400.hdmi"
CARD1 = "alsa_card.platform-107c706400.hdmi"

PROFILE_STEREO = "output:hdmi-stereo"
PROFILE_PCM51 = "output:hdmi-surround"
PROFILE_AC3 = "output:hdmi-ac3"
PROFILE_DTS = "output:hdmi-dts"

MODE_STEREO = "stereo"
MODE_PCM51 = "pcm51"
MODE_AC3 = "ac3"
MODE_DTS = "dts"

HOLDING_SINK = "rpi_hdmi_audio_holding"
HOLDING_DESCRIPTION = "HDMI"

CHANNEL_VOLUME_MIN = 0
CHANNEL_VOLUME_MAX = 150
CHANNEL_VOLUME_DEFAULT = 100
MASTER_VOLUME_MIN = 0
MASTER_VOLUME_MAX = 100
MASTER_VOLUME_DEFAULT = 100

CHANNEL_KEYS = (
    "front-left",
    "front-right",
    "rear-left",
    "rear-right",
    "front-center",
    "lfe",
)

VOLUME_PANEL = (
    ("volume", "Volume"),
    ("front-left", "Front Left"),
    ("front-right", "Front Right"),
    ("front-center", "Center"),
    ("rear-left", "Rear Left"),
    ("rear-right", "Rear Right"),
    ("lfe", "Subwoofer"),
)


# ---------------------------------------------------------------------------
# The direct ALSA sink is GNOME's master volume. A single PipeWire smart
# filter owns the six independent speaker gains and their Frequency Balance.
# Encoded outputs keep the filter connected at unity so moving a slider cannot
# move an already running AC-3/DTS carrier. Flat PCM outputs retain v16.3's
# direct path until a trim or Frequency Balance setting needs the filter.
# ---------------------------------------------------------------------------

FREQUENCY_MIN = 0
FREQUENCY_MAX = 100
FREQUENCY_DEFAULT = 100
FREQUENCY_FILTER_FLOOR_DB = -36.0
FREQUENCY_FILTER_CHAIN_UNIT = "filter-chain.service"
FREQUENCY_FILTER_CONFIG_DIR = (
    Path.home() / ".config" / "pipewire" / "filter-chain.conf.d"
)
FREQUENCY_FILTER_CONFIG_FILE = (
    FREQUENCY_FILTER_CONFIG_DIR / "99-rpi-hdmi-audio-frequency.conf"
)

# (range label, friendly name, PipeWire biquad, centre/corner Hz, Q)
# v13 keeps the crossover feeling smooth, but the six peaking bands use a much
# narrower Q than v12. This prevents a deep cut in one slider from dragging the
# neighbouring slider down with it. The 0% floor is -36 dB instead of -60 dB;
# that is effectively silent for the selected band while greatly reducing the
# very broad skirts caused by an extreme -60 dB notch. At 100% a biquad has
# 0 dB gain, while the graph stays connected for live edits.
FREQUENCY_BANDS = (
    ("20–60 Hz", "Deep Bass", "bq_lowshelf", 60, 0.7071),
    ("60–120 Hz", "Bass", "bq_peaking", 85, 4.0000),
    ("120–250 Hz", "Upper Bass", "bq_peaking", 173, 4.0000),
    ("250–500 Hz", "Low Mid", "bq_peaking", 354, 4.0000),
    ("500–1,000 Hz", "Mid", "bq_peaking", 707, 4.0000),
    ("1–2 kHz", "Upper Mid", "bq_peaking", 1414, 4.0000),
    ("2–4 kHz", "Presence", "bq_peaking", 2828, 4.0000),
    ("4–20 kHz", "High", "bq_highshelf", 4000, 0.7071),
)

CHANNEL_TITLES = {
    "front-left": "Front Left",
    "front-right": "Front Right",
    "front-center": "Center",
    "rear-left": "Rear Left",
    "rear-right": "Rear Right",
    "lfe": "Subwoofer",
}

CHANNEL_FILTER_PREFIX = {
    "front-left": "fl",
    "front-right": "fr",
    "rear-left": "rl",
    "rear-right": "rr",
    "front-center": "fc",
    "lfe": "lfe",
}

CHANNEL_PIPEWIRE_POSITION = {
    "front-left": "FL",
    "front-right": "FR",
    "rear-left": "RL",
    "rear-right": "RR",
    "front-center": "FC",
    "lfe": "LFE",
}

FREQUENCY_FILTERS = {
    "stereo": {
        "node": "rpi_hdmi_audio_frequency_stereo",
        "output": "rpi_hdmi_audio_frequency_stereo_out",
        "smart": "rpi-hdmi-audio-frequency-stereo",
        "link": "rpi-hdmi-audio-frequency-stereo-link",
        "channels": ("front-left", "front-right"),
    },
    "surround": {
        "node": "rpi_hdmi_audio_frequency_surround",
        "output": "rpi_hdmi_audio_frequency_surround_out",
        "smart": "rpi-hdmi-audio-frequency-surround",
        "link": "rpi-hdmi-audio-frequency-surround-link",
        # Keep the exact v10 order used by the direct 5.1 sinks.
        "channels": (
            "front-left",
            "front-right",
            "rear-left",
            "rear-right",
            "front-center",
            "lfe",
        ),
    },
}

# A short-lived per-process cache keeps continuous slider edits from dumping
# the entire PipeWire graph on every step. A service restart invalidates it.
ACTIVE_DSP_NODES: dict[str, tuple[int, float]] = {}


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
    mmap: bool = True


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
        key="hdmi0-pcm51",
        title="HDMI 0 · PCM 5.1",
        subtitle="Uncompressed PCM 5.1 for HDMI → HDMI",
        card=CARD0,
        other_card=CARD1,
        mode=MODE_PCM51,
        profile=PROFILE_PCM51,
        alsa_device="hdmi:CARD=vc4hdmi0,DEV=0",
        sink_name="rpi_hdmi_audio_hdmi0_pcm51",
        description="HDMI 0 - PCM 5.1",
        channels=6,
        channel_map=(
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
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
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
    ),
    Choice(
        key="hdmi0-dts",
        title="HDMI 0 · DTS 5.1",
        subtitle="Realtime DTS 5.1 for HDMI → TOSLINK → receiver",
        card=CARD0,
        other_card=CARD1,
        mode=MODE_DTS,
        profile=PROFILE_DTS,
        alsa_device="dcahdmi:CARD=vc4hdmi0,DEV=0,IEC61937=1",
        sink_name="rpi_hdmi_audio_hdmi0_dts",
        description="HDMI 0 - DTS 5.1",
        channels=6,
        channel_map=(
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
        mmap=False,
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
        key="hdmi1-pcm51",
        title="HDMI 1 · PCM 5.1",
        subtitle="Uncompressed PCM 5.1 for HDMI → HDMI",
        card=CARD1,
        other_card=CARD0,
        mode=MODE_PCM51,
        profile=PROFILE_PCM51,
        alsa_device="hdmi:CARD=vc4hdmi1,DEV=0",
        sink_name="rpi_hdmi_audio_hdmi1_pcm51",
        description="HDMI 1 - PCM 5.1",
        channels=6,
        channel_map=(
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
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
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
    ),
    Choice(
        key="hdmi1-dts",
        title="HDMI 1 · DTS 5.1",
        subtitle="Realtime DTS 5.1 for HDMI → TOSLINK → receiver",
        card=CARD1,
        other_card=CARD0,
        mode=MODE_DTS,
        profile=PROFILE_DTS,
        alsa_device="dcahdmi:CARD=vc4hdmi1,DEV=0,IEC61937=1",
        sink_name="rpi_hdmi_audio_hdmi1_dts",
        description="HDMI 1 - DTS 5.1",
        channels=6,
        channel_map=(
            "front-left,front-right,rear-left,rear-right,front-center,lfe"
        ),
        mmap=False,
    ),
)

CHOICE_BY_KEY = {choice.key: choice for choice in CHOICES}
CHOICE_BY_SINK = {choice.sink_name: choice for choice in CHOICES}

LEGACY_SINKS = {
    "rpi_test_ac3": "hdmi0-ac3",
    "rpi_test_dts": "hdmi0-dts",
    "hdmi0_ac3": "hdmi0-ac3",
    "hdmi1_ac3": "hdmi1-ac3",
    "hdmi0_dts": "hdmi0-dts",
    "hdmi1_dts": "hdmi1-dts",
    "hdmi0_stereo": "hdmi0-stereo",
    "hdmi1_stereo": "hdmi1-stereo",
    "hdmi0_pcm51": "hdmi0-pcm51",
    "hdmi1_pcm51": "hdmi1-pcm51",
}

OUTPUT_SINK_NAMES = set(CHOICE_BY_SINK) | set(LEGACY_SINKS)


def read_audio_state() -> tuple[
    Choice | None,
    dict[str, int],
    int,
]:
    volumes = {
        key: CHANNEL_VOLUME_DEFAULT
        for key in CHANNEL_KEYS
    }
    master_volume = MASTER_VOLUME_DEFAULT

    try:
        lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None, volumes, master_volume

    choice = CHOICE_BY_KEY.get(lines[0].strip()) if lines else None

    for raw in lines[1:]:
        key, separator, raw_value = raw.partition("=")

        if separator != "=":
            continue

        try:
            value = int(round(float(raw_value.strip())))
        except ValueError:
            continue

        if key == "volume":
            master_volume = max(
                MASTER_VOLUME_MIN,
                min(MASTER_VOLUME_MAX, value),
            )
        elif key in volumes:
            volumes[key] = max(
                CHANNEL_VOLUME_MIN,
                min(CHANNEL_VOLUME_MAX, value),
            )

    return choice, volumes, master_volume


def load_choice_state() -> Choice | None:
    choice, _, _ = read_audio_state()
    return choice


def load_channel_volume_state() -> dict[str, int]:
    _, volumes, _ = read_audio_state()
    return volumes


def load_master_volume_state() -> int:
    _, _, master_volume = read_audio_state()
    return master_volume


def load_frequency_balance_state() -> dict[str, list[int]]:
    balance = {
        key: [FREQUENCY_DEFAULT] * len(FREQUENCY_BANDS)
        for key in CHANNEL_KEYS
    }

    try:
        lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return balance

    for raw in lines[1:]:
        key, separator, raw_value = raw.partition("=")
        if separator != "=":
            continue

        if key.startswith("frequency-"):
            channel_key = key.removeprefix("frequency-")
        elif key.startswith("frequency."):
            channel_key = key.removeprefix("frequency.")
        else:
            continue
        if channel_key not in balance:
            continue

        parts = [part.strip() for part in raw_value.split(",")]
        if len(parts) != len(FREQUENCY_BANDS):
            continue

        parsed: list[int] = []
        valid = True
        for part in parts:
            try:
                value = int(round(float(part)))
            except ValueError:
                valid = False
                break
            parsed.append(
                max(FREQUENCY_MIN, min(FREQUENCY_MAX, value))
            )

        if valid:
            balance[channel_key] = parsed

    return balance


CHANNEL_VOLUMES = load_channel_volume_state()
MASTER_VOLUME = load_master_volume_state()
FREQUENCY_BALANCE = load_frequency_balance_state()


def save_audio_state(
    choice: Choice | None = None,
    *,
    fallback_choice: Choice | None = None,
    master_volume: int | None = None,
    channel_volumes: dict[str, int] | None = None,
    frequency_balance: dict[str, list[int]] | None = None,
) -> None:
    """Atomically merge only the settings changed by this caller.

    The GUI and restore monitor run in separate processes. A stable lock keeps
    their read/modify/write operations in order; a unique temporary file keeps
    an interrupted writer from damaging the previously saved state.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "last-choice.lock"

    with lock_path.open("a+b") as lock_handle:
        os.fchmod(lock_handle.fileno(), 0o600)
        fcntl.flock(lock_handle, fcntl.LOCK_EX)

        saved_choice, saved_volumes, saved_master = read_audio_state()
        saved_frequency = load_frequency_balance_state()

        selected_choice = (
            choice
            or saved_choice
            or fallback_choice
            or CHOICE_BY_KEY["hdmi0-stereo"]
        )

        if master_volume is not None:
            saved_master = max(
                MASTER_VOLUME_MIN,
                min(MASTER_VOLUME_MAX, int(master_volume)),
            )

        for key, value in (channel_volumes or {}).items():
            if key not in saved_volumes:
                raise ValueError(f"Unknown audio channel: {key}")
            saved_volumes[key] = max(
                CHANNEL_VOLUME_MIN,
                min(CHANNEL_VOLUME_MAX, int(value)),
            )

        for key, values in (frequency_balance or {}).items():
            if key not in saved_frequency or len(values) != len(FREQUENCY_BANDS):
                raise ValueError(f"Invalid Frequency Balance channel: {key}")
            saved_frequency[key] = [
                max(FREQUENCY_MIN, min(FREQUENCY_MAX, int(value)))
                for value in values
            ]

        lines = [
            selected_choice.key,
            f"volume={saved_master}",
            *(f"{key}={saved_volumes[key]}" for key in CHANNEL_KEYS),
            *(
                "frequency-"
                + key
                + "="
                + ",".join(str(value) for value in saved_frequency[key])
                for key in CHANNEL_KEYS
            ),
        ]

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=STATE_DIR,
                prefix="last-choice.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temporary, STATE_FILE)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


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


@contextmanager
def audio_operation_lock():
    """Serialize graph changes across the desktop window and restore monitor."""
    with _operation_thread_lock:
        depth = getattr(_operation_depth, "value", 0)
        if depth:
            _operation_depth.value = depth + 1
            try:
                yield
            finally:
                _operation_depth.value -= 1
            return

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with OPERATION_LOCK_FILE.open("a+b") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            _operation_depth.value = 1
            try:
                yield
            finally:
                _operation_depth.value = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Channel trim and Frequency Balance: one PipeWire smart filter
# ---------------------------------------------------------------------------


def frequency_percent_to_db(percent: int) -> float:
    percent = max(FREQUENCY_MIN, min(FREQUENCY_MAX, int(percent)))
    if percent <= 0:
        return FREQUENCY_FILTER_FLOOR_DB
    return max(
        FREQUENCY_FILTER_FLOOR_DB,
        20.0 * math.log10(percent / 100.0),
    )


def channel_percent_to_multiplier(percent: int) -> float:
    """Match the cubic PulseAudio percentage used by pactl and GNOME."""
    percent = max(CHANNEL_VOLUME_MIN, min(CHANNEL_VOLUME_MAX, int(percent)))
    return (percent / 100.0) ** 3


def frequency_values_for_channel(
    channel_key: str,
    override: dict[str, list[int]] | None = None,
) -> list[int]:
    if override is not None and channel_key in override:
        values = override[channel_key]
    else:
        values = FREQUENCY_BALANCE[channel_key]

    return [
        max(FREQUENCY_MIN, min(FREQUENCY_MAX, int(round(value))))
        for value in values
    ]


def frequency_filter_kind(choice: Choice) -> str:
    return "stereo" if choice.channels == 2 else "surround"


def dsp_requested(
    choice: Choice,
    override: dict[str, list[int]] | None = None,
) -> bool:
    """Only encoded modes need a unity DSP before the first live edit."""
    if choice.mode in (MODE_AC3, MODE_DTS):
        return True
    channels = FREQUENCY_FILTERS[frequency_filter_kind(choice)]["channels"]
    return any(
        CHANNEL_VOLUMES[key] != CHANNEL_VOLUME_DEFAULT
        or any(value != FREQUENCY_DEFAULT
               for value in frequency_values_for_channel(key, override))
        for key in channels
    )


def _render_frequency_filter_module(kind: str) -> str:
    spec = FREQUENCY_FILTERS[kind]
    channel_keys = tuple(spec["channels"])
    positions = " ".join(
        CHANNEL_PIPEWIRE_POSITION[key]
        for key in channel_keys
    )

    nodes: list[str] = []
    links: list[str] = []
    inputs: list[str] = []
    outputs: list[str] = []

    for channel_key in channel_keys:
        prefix = CHANNEL_FILTER_PREFIX[channel_key]
        values = FREQUENCY_BALANCE[channel_key]

        for index, (_, _, label, frequency, q_value) in enumerate(
            FREQUENCY_BANDS,
            start=1,
        ):
            node_name = f"{prefix}_b{index}"
            gain_db = frequency_percent_to_db(values[index - 1])
            nodes.append(
                "                    { "
                f"type = builtin name = {node_name} label = {label} "
                "control = { "
                f'"Freq" = {frequency} '
                f'"Q" = {q_value:.4f} '
                f'"Gain" = {gain_db:.4f} '
                "} }"
            )

            if index > 1:
                previous = f"{prefix}_b{index - 1}"
                links.append(
                    "                    { "
                    f'output = "{previous}:Out" '
                    f'input = "{node_name}:In" '
                    "}"
                )

        inputs.append(f'"{prefix}_b1:In"')
        gain_node = f"{prefix}_volume"
        nodes.append(
            "                    { "
            f"type = builtin name = {gain_node} label = linear "
            "control = { "
            f'"Mult" = {channel_percent_to_multiplier(CHANNEL_VOLUMES[channel_key]):.6f} '
            '"Add" = 0.0 '
            "} }"
        )
        links.append(
            "                    { "
            f'output = "{prefix}_b{len(FREQUENCY_BANDS)}:Out" '
            f'input = "{gain_node}:In" '
            "}"
        )
        outputs.append(f'"{gain_node}:Out"')

    return f"""    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "HDMI Audio Encoder Channel and Frequency"
            media.name = "HDMI Audio Encoder Channel and Frequency"
            audio.channels = {len(channel_keys)}
            audio.position = [ {positions} ]
            filter.graph = {{
                nodes = [
{chr(10).join(nodes)}
                ]
                links = [
{chr(10).join(links)}
                ]
                inputs = [ {' '.join(inputs)} ]
                outputs = [ {' '.join(outputs)} ]
            }}
            capture.props = {{
                node.name = "{spec['node']}"
                node.description = "HDMI Audio Encoder Channel and Frequency"
                rpi.hdmi.audio.dsp.version = "{DSP_CONFIG_REVISION}"
                media.class = Audio/Sink
                device.class = "filter"
                device.api = "virtual"
                node.virtual = true
                node.link-group = "{spec['link']}"
                filter.smart = true
                filter.smart.name = "{spec['smart']}"
                filter.smart.disabled = true
                filter.smart.targetable = false
                session.suspend-timeout-seconds = 0
            }}
            playback.props = {{
                node.name = "{spec['output']}"
                device.class = "filter"
                device.api = "virtual"
                node.virtual = true
                node.link-group = "{spec['link']}"
                node.passive = true
                stream.dont-remix = true
                session.suspend-timeout-seconds = 0
            }}
        }}
    }}"""


def render_frequency_filter_config() -> str:
    modules = "\n".join(
        _render_frequency_filter_module(kind)
        for kind in ("stereo", "surround")
    )
    return f"""# HDMI Audio Encoder v{APP_VERSION} - generated file
# Channel trims and Frequency Balance share a WirePlumber smart filter.
# Encoded layouts remain connected at unity to protect the carrier; flat PCM
# bypasses this graph. GNOME controls the uniform real ALSA sink volume.
context.modules = [
{modules}
]
"""


def write_frequency_filter_config() -> bool:
    text = render_frequency_filter_config()
    FREQUENCY_FILTER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if (
            FREQUENCY_FILTER_CONFIG_FILE.is_file()
            and FREQUENCY_FILTER_CONFIG_FILE.read_text(encoding="utf-8") == text
        ):
            return False
    except OSError:
        pass

    temporary = FREQUENCY_FILTER_CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(FREQUENCY_FILTER_CONFIG_FILE)
    return True


def frequency_pipewire_tools_available() -> bool:
    return all(
        shutil.which(tool) is not None
        for tool in ("pw-dump", "pw-cli", "pw-metadata")
    )


def frequency_pipewire_dump() -> list[dict]:
    result = run(["pw-dump"], check=False)
    if result.returncode != 0:
        return []

    try:
        parsed = json.loads(result.stdout or "[]")
    except ValueError:
        return []

    return parsed if isinstance(parsed, list) else []


def _frequency_node_ids(
    objects: list[dict] | None = None,
    *,
    accept_legacy: bool = False,
) -> dict[str, int]:
    objects = frequency_pipewire_dump() if objects is None else objects
    wanted = {
        spec["node"]: kind
        for kind, spec in FREQUENCY_FILTERS.items()
    }
    found: dict[str, int] = {}

    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if not accept_legacy and props.get("rpi.hdmi.audio.dsp.version") != DSP_CONFIG_REVISION:
            continue
        node_name = props.get("node.name")
        kind = wanted.get(node_name)
        node_id = obj.get("id")
        if kind is not None and isinstance(node_id, int):
            found[kind] = node_id

    return found


def _frequency_metadata_value(
    objects: list[dict],
    metadata_name: str,
    subject: int,
    key: str,
):
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if (obj.get("props") or {}).get("metadata.name") != metadata_name:
            continue
        for entry in obj.get("metadata") or []:
            if entry.get("subject") == subject and entry.get("key") == key:
                value = entry.get("value")
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except ValueError:
                        pass
                return value
    return None


def _frequency_set_metadata_json(
    node_id: int,
    key: str,
    value,
    objects: list[dict] | None = None,
) -> None:
    if objects is not None:
        current = _frequency_metadata_value(
            objects,
            "filters",
            node_id,
            key,
        )
        if current == value:
            return

    encoded = json.dumps(value, separators=(",", ":"))
    result = run(
        [
            "pw-metadata",
            "-n",
            "filters",
            str(node_id),
            key,
            encoded,
            "Spa:String:JSON",
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Could not set PipeWire filter metadata {key}: {detail}"
        )


def _frequency_set_disabled(
    node_id: int,
    disabled: bool,
    objects: list[dict] | None = None,
) -> None:
    _frequency_set_metadata_json(
        node_id,
        "filter.smart.disabled",
        bool(disabled),
        objects,
    )


def _frequency_set_target(
    node_id: int,
    sink_name: str,
    objects: list[dict] | None = None,
) -> None:
    _frequency_set_metadata_json(
        node_id,
        "filter.smart.target",
        {"node.name": sink_name},
        objects,
    )


def _frequency_apply_gains(
    node_id: int,
    channel_keys: tuple[str, ...],
    override: dict[str, list[int]] | None = None,
) -> None:
    params: list[str] = []

    for channel_key in channel_keys:
        prefix = CHANNEL_FILTER_PREFIX[channel_key]
        params.append(
            f'"{prefix}_volume:Mult" '
            f"{channel_percent_to_multiplier(CHANNEL_VOLUMES[channel_key]):.6f}"
        )
        values = frequency_values_for_channel(channel_key, override)
        for index, percent in enumerate(values, start=1):
            params.append(
                f'"{prefix}_b{index}:Gain" '
                f"{frequency_percent_to_db(percent):.4f}"
            )

    result = run(
        [
            "pw-cli",
            "set-param",
            str(node_id),
            "Props",
            "{ params = [ " + " ".join(params) + " ] }",
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not apply Frequency Balance: " + detail
        )


def ensure_frequency_filter_nodes(
    allow_restart: bool = True,
) -> tuple[dict[str, int], list[dict]]:
    if not frequency_pipewire_tools_available():
        return {}, []

    objects = frequency_pipewire_dump()
    node_ids = _frequency_node_ids(objects)

    if len(node_ids) == len(FREQUENCY_FILTERS) or not allow_restart:
        return node_ids, objects

    # Keep the direct ALSA sink usable if the optional DSP service cannot start.
    write_frequency_filter_config()
    run(
        ["systemctl", "--user", "restart", FREQUENCY_FILTER_CHAIN_UNIT],
        check=False,
    )

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        objects = frequency_pipewire_dump()
        node_ids = _frequency_node_ids(objects)
        if len(node_ids) == len(FREQUENCY_FILTERS):
            break
        time.sleep(0.10)

    return node_ids, objects


def disable_frequency_filters() -> None:
    ACTIVE_DSP_NODES.clear()
    if not frequency_pipewire_tools_available():
        return

    objects = frequency_pipewire_dump()
    node_ids = _frequency_node_ids(objects, accept_legacy=True)
    for node_id in node_ids.values():
        try:
            _frequency_set_disabled(node_id, True, objects)
        except Exception:
            pass


def apply_frequency_filter_for_choice(
    choice: Choice,
    override: dict[str, list[int]] | None = None,
    allow_restart: bool = True,
) -> bool:
    """Connect the DSP before audio leaves the holding sink."""
    active_kind = frequency_filter_kind(choice)

    if not dsp_requested(choice, override):
        node_ids, objects = ensure_frequency_filter_nodes(allow_restart=False)
        active_id = node_ids.get(active_kind)
        if active_id is not None and (
            _frequency_metadata_value(
                objects, "filters", active_id, "filter.smart.disabled"
            ) is False
            and _frequency_metadata_value(
                objects, "filters", active_id, "filter.smart.target"
            ) == {"node.name": choice.sink_name}
        ):
            # Once PCM entered the DSP path, return all controls to unity
            # without moving an already playing stream. The next output
            # switch restores the fully direct flat PCM route.
            _frequency_apply_gains(
                active_id,
                tuple(FREQUENCY_FILTERS[active_kind]["channels"]),
                override,
            )
            ACTIVE_DSP_NODES[choice.sink_name] = (
                active_id, time.monotonic()
            )
            return True
        ACTIVE_DSP_NODES.pop(choice.sink_name, None)
        for node_id in node_ids.values():
            _frequency_set_disabled(node_id, True, objects)
        return False

    node_ids, objects = ensure_frequency_filter_nodes(allow_restart)
    if not node_ids:
        return False

    # Keep the filter for the other channel layout out of the graph.
    for kind, node_id in node_ids.items():
        if kind == active_kind:
            continue
        _frequency_set_disabled(node_id, True, objects)

    active_id = node_ids.get(active_kind)
    if active_id is None:
        return False

    spec = FREQUENCY_FILTERS[active_kind]
    channel_keys = tuple(spec["channels"])

    # At 100% the DSP remains connected. Live slider changes update controls,
    # not the graph's metadata, so encoded keepalive does not move mid-stream.
    _frequency_set_target(active_id, choice.sink_name, objects)
    _frequency_apply_gains(active_id, channel_keys, override)
    _frequency_set_disabled(active_id, False, objects)
    ACTIVE_DSP_NODES.clear()
    ACTIVE_DSP_NODES[choice.sink_name] = (active_id, time.monotonic())
    return True


def safe_apply_frequency_filter_for_choice(
    choice: Choice,
    override: dict[str, list[int]] | None = None,
    allow_restart: bool = True,
) -> tuple[bool, str | None]:
    with audio_operation_lock():
        try:
            ok = apply_frequency_filter_for_choice(
                choice,
                override=override,
                allow_restart=allow_restart,
            )
            if ok:
                return True, None
            ACTIVE_DSP_NODES.pop(choice.sink_name, None)
            if not dsp_requested(choice, override):
                return False, None
            return False, "PipeWire filter is not available."
        except Exception as exc:
            ACTIVE_DSP_NODES.pop(choice.sink_name, None)
            try:
                disable_frequency_filters()
            except Exception:
                pass
            return False, str(exc)


def sink_properties_argument(
    description: str,
    keep_open: bool = False,
) -> str:
    escaped = (
        description
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    properties = [f'device.description="{escaped}"']

    if keep_open:
        properties.extend(
            [
                "session.suspend-timeout-seconds=0",
                "node.suspend-on-idle=false",
            ]
        )

    return "sink_properties='" + " ".join(properties) + "'"


def get_sinks() -> dict[str, str]:
    result = run(
        ["pactl", "list", "sinks", "short"],
        check=False,
    )

    sinks: dict[str, str] = {}

    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            sinks[fields[1].strip()] = fields[0].strip()

    return sinks


def get_sink_inputs() -> dict[str, str]:
    result = run(
        ["pactl", "list", "sink-inputs", "short"],
        check=False,
    )

    inputs: dict[str, str] = {}

    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0].strip().isdigit():
            inputs[fields[0].strip()] = fields[1].strip()

    return inputs


def get_module_lines() -> list[str]:
    result = run(
        ["pactl", "list", "modules", "short"],
        check=False,
    )
    return result.stdout.splitlines()


def get_card_profiles() -> dict[str, str]:
    result = run(
        ["pactl", "list", "cards"],
        check=False,
    )

    profiles: dict[str, str] = {}
    current_name: str | None = None

    for raw in result.stdout.splitlines():
        line = raw.strip()

        if line.startswith("Name: "):
            current_name = line.removeprefix("Name: ").strip()

        elif line.startswith("Active Profile: ") and current_name:
            profiles[current_name] = (
                line.removeprefix("Active Profile: ").strip()
            )
            current_name = None

    return profiles


def _alsa_card_id_for_choice(choice: Choice) -> str:
    return "vc4hdmi0" if choice.card == CARD0 else "vc4hdmi1"


def hdmi_port_connected(
    choice: Choice,
    drm_root: Path = Path("/sys/class/drm"),
) -> bool:
    # Card numbers may change at boot. HDMI-A-1 and HDMI-A-2 identify the
    # physical HDMI 0 and HDMI 1 connectors on the supported Raspberry Pis.
    connector = "HDMI-A-1" if choice.card == CARD0 else "HDMI-A-2"

    try:
        for status_path in drm_root.glob(f"card*-{connector}/status"):
            if status_path.read_text(encoding="ascii").strip() == "connected":
                return True
    except OSError:
        return False

    return False


def hdmi_pcm51_capability(
    choice: Choice,
    proc_root: Path = Path("/proc/asound"),
) -> bool | None:
    """Return True/False when ELD clearly says whether 6ch LPCM is supported.

    None means the kernel ELD information is temporarily unavailable. This is
    only displayed in the GUI; the user may select PCM 5.1 in either case.
    """
    wanted_id = _alsa_card_id_for_choice(choice)

    try:
        card_dirs = list(proc_root.glob("card[0-9]*"))
    except OSError:
        return None

    for card_dir in card_dirs:
        try:
            card_id = (card_dir / "id").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue

        if card_id != wanted_id:
            continue

        valid_eld_seen = False

        for eld_path in card_dir.glob("eld#*"):
            try:
                text = eld_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if not re.search(r"^monitor_present\s+1$", text, re.MULTILINE):
                continue
            if not re.search(r"^eld_valid\s+1$", text, re.MULTILINE):
                continue

            valid_eld_seen = True

            for match in re.finditer(
                r"^sad(\d+)_coding_type\s+.*LPCM.*$",
                text,
                re.MULTILINE,
            ):
                sad_index = match.group(1)
                channels_match = re.search(
                    rf"^sad{sad_index}_channels\s+(\d+)$",
                    text,
                    re.MULTILINE,
                )
                if channels_match and int(channels_match.group(1)) >= 6:
                    return True

        if valid_eld_seen:
            return False

        return None

    return None


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
            profiles.get(choice.card) == choice.profile
            and profiles.get(choice.other_card, "off") == "off"
        ):
            return choice

    return None


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

        if not any(
            f"sink_name={sink_name}" in line
            for sink_name in sink_names
        ):
            continue

        fields = line.split("\t", 1)
        if fields and fields[0].strip().isdigit():
            module_ids.append(fields[0].strip())

    return module_ids



def reload_volume_state_from_disk() -> Choice | None:
    global MASTER_VOLUME

    choice, volumes, master_volume = read_audio_state()

    CHANNEL_VOLUMES.clear()
    CHANNEL_VOLUMES.update(volumes)
    MASTER_VOLUME = master_volume

    frequency = load_frequency_balance_state()
    FREQUENCY_BALANCE.clear()
    FREQUENCY_BALANCE.update(frequency)

    return choice


def get_sink_channel_volumes(
    choice: Choice,
) -> tuple[int, ...] | None:
    result = run(
        [
            "pactl",
            "get-sink-volume",
            choice.sink_name,
        ],
        check=False,
    )

    if result.returncode != 0:
        return None

    values = tuple(
        int(value)
        for value in re.findall(
            r"(\d+)%",
            result.stdout,
        )
    )

    if len(values) < choice.channels:
        return None

    return values[:choice.channels]


def volumes_are_close(
    first: tuple[int, ...],
    second: tuple[int, ...],
    tolerance: int = 0,
) -> bool:
    if len(first) != len(second):
        return False

    return all(
        abs(left - right) <= tolerance
        for left, right in zip(first, second)
    )


def derive_external_master_volume(
    choice: Choice,
    actual: tuple[int, ...],
    *,
    filtered: bool = False,
) -> int | None:
    """Return a new master volume only for a genuine master-volume change.

    HDMI Audio Encoder owns the six channel trims. GNOME's normal master-volume
    control is allowed to change the master, but Balance/Fade/Subwoofer edits
    are asymmetric channel changes and must never overwrite our saved values.
    """
    if not actual:
        return None

    keys = choice_volume_keys(choice)

    if filtered:
        # The hardware sink has one uniform GNOME master. The six speaker
        # ratios live only in the DSP graph, never in pactl's sink volume.
        if max(actual) - min(actual) > 1:
            return None
        candidate = int(round(sum(actual) / len(actual)))
        return max(MASTER_VOLUME_MIN, min(MASTER_VOLUME_MAX, candidate))

    # When every active speaker trim in our app is intentionally 0%, the real
    # sink is also 0% on every channel. That does not mean GNOME changed the
    # master Volume to 0%; keep the saved master until it is explicitly changed.
    if max(actual) <= 1 and all(CHANNEL_VOLUMES[key] <= 0 for key in keys):
        return None

    # GNOME Shell commonly writes one identical value to every channel. This is
    # unambiguously a master-volume change, even when our own trims are unequal.
    if max(actual) - min(actual) <= 1:
        candidate = int(round(sum(actual) / len(actual)))
        return max(MASTER_VOLUME_MIN, min(MASTER_VOLUME_MAX, candidate))

    # Some clients preserve the existing channel ratios while scaling the whole
    # sink. Reverse our trims and accept the result only when all usable channels
    # agree on essentially the same master value.
    candidates: list[float] = []
    for key, channel_value in zip(keys, actual):
        trim = CHANNEL_VOLUMES[key]
        if trim <= 0:
            continue

        # A channel clipped at our 150% ceiling cannot accurately reveal the
        # master value, so leave saturated samples out of this test.
        if channel_value >= CHANNEL_VOLUME_MAX:
            continue

        candidates.append(channel_value * 100.0 / trim)

    if not candidates:
        return None

    candidates.sort()
    middle = len(candidates) // 2
    if len(candidates) % 2:
        centre = candidates[middle]
    else:
        centre = (candidates[middle - 1] + candidates[middle]) / 2.0

    tolerance = max(2.0, centre * 0.03)
    if any(abs(value - centre) > tolerance for value in candidates):
        # Asymmetric change: GNOME Balance/Fade/Subwoofer. Our saved six-channel
        # trims remain authoritative and the caller will immediately restore them.
        return None

    candidate = int(round(centre))
    return max(MASTER_VOLUME_MIN, min(MASTER_VOLUME_MAX, candidate))


def current_expected_sink_volumes(
    choice: Choice,
    *,
    filtered: bool = False,
) -> tuple[int, ...]:
    if filtered:
        return (MASTER_VOLUME,) * choice.channels
    return tuple(
        effective_channel_volume(key)
        for key in choice_volume_keys(choice)
    )


def choice_volume_keys(choice: Choice) -> tuple[str, ...]:
    if choice.channels == 2:
        return ("front-left", "front-right")

    return tuple(
        part.strip()
        for part in choice.channel_map.split(",")
        if part.strip()
    )


def effective_channel_volume(channel_key: str) -> int:
    value = int(
        round(
            MASTER_VOLUME
            * CHANNEL_VOLUMES[channel_key]
            / 100.0
        )
    )

    return max(
        CHANNEL_VOLUME_MIN,
        min(CHANNEL_VOLUME_MAX, value),
    )


def apply_channel_volumes_to_sink(
    choice: Choice,
    *,
    filtered: bool = False,
) -> None:
    expected = current_expected_sink_volumes(choice, filtered=filtered)
    if get_sink_channel_volumes(choice) == expected:
        return
    values = [f"{value}%" for value in expected]

    result = run(
        [
            "pactl",
            "set-sink-volume",
            choice.sink_name,
            *values,
        ],
        check=False,
    )

    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Unknown PipeWire error."
        )
        raise RuntimeError(
            "Could not apply volume: " + detail
        )


def frequency_filter_is_active(choice: Choice) -> bool:
    """Use this for monitoring GNOME edits in either the DSP or fallback mode."""
    if not frequency_pipewire_tools_available():
        return False
    objects = frequency_pipewire_dump()
    node_id = _frequency_node_ids(objects).get(frequency_filter_kind(choice))
    if node_id is None:
        return False
    active = (
        _frequency_metadata_value(
            objects, "filters", node_id, "filter.smart.disabled"
        ) is False
        and _frequency_metadata_value(
            objects, "filters", node_id, "filter.smart.target"
        ) == {"node.name": choice.sink_name}
    )
    if active:
        ACTIVE_DSP_NODES[choice.sink_name] = (node_id, time.monotonic())
    else:
        ACTIVE_DSP_NODES.pop(choice.sink_name, None)
    return active


def apply_audio_controls(
    choice: Choice,
    *,
    override: dict[str, list[int]] | None = None,
    allow_restart: bool = False,
) -> tuple[bool, str | None]:
    """Use DSP gains with a uniform master, or a working direct fallback."""
    with audio_operation_lock():
        filtered, error = safe_apply_frequency_filter_for_choice(
            choice, override=override, allow_restart=allow_restart
        )
        apply_channel_volumes_to_sink(choice, filtered=filtered)
        return filtered, error


def apply_live_audio_controls(
    choice: Choice,
    *,
    override: dict[str, list[int]] | None = None,
    master_changed: bool = False,
    speaker_changed: bool = True,
) -> tuple[bool, str | None]:
    """Change Props only during a drag; recheck the graph periodically."""
    cached = ACTIVE_DSP_NODES.get(choice.sink_name)
    cache_valid = cached is not None and time.monotonic() - cached[1] < 2.0
    if master_changed and not speaker_changed and (
        cache_valid or not dsp_requested(choice, override)
    ):
        filtered = bool(cache_valid)
        with audio_operation_lock():
            apply_channel_volumes_to_sink(choice, filtered=filtered)
        return filtered, None
    if (
        not master_changed and cache_valid
        and dsp_requested(choice, override)
    ):
        try:
            with audio_operation_lock():
                _frequency_apply_gains(
                    cached[0],
                    tuple(FREQUENCY_FILTERS[frequency_filter_kind(choice)]["channels"]),
                    override,
                )
            return True, None
        except Exception:
            ACTIVE_DSP_NODES.pop(choice.sink_name, None)
    return apply_audio_controls(choice, override=override)


def _set_sink_channel_volumes(
    choice: Choice,
    values: tuple[int, ...],
) -> None:
    result = run(
        [
            "pactl",
            "set-sink-volume",
            choice.sink_name,
            *(f"{value}%" for value in values),
        ],
        check=False,
    )

    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Unknown PipeWire error."
        )
        raise RuntimeError(
            "Could not wake the selected HDMI output: " + detail
        )


def wake_selected_output(choice: Choice, *, filtered: bool = False) -> None:
    """Actively wake a freshly switched sink.

    On some HDMI -> optical -> receiver paths the newly loaded ALSA sink exists
    and application streams are attached, but no samples reach the receiver
    until the sink receives another volume operation. v13 performs that harmless
    wake-up automatically instead of making the user touch a slider.
    """
    if choice.sink_name not in get_sinks():
        return

    # Make sure PipeWire has not left the fresh ALSA sink suspended.
    run(
        ["pactl", "suspend-sink", choice.sink_name, "0"],
        check=False,
    )

    if (
        choice.mode in (MODE_AC3, MODE_DTS)
        and not find_encoded_keepalive_pids()
    ):
        start_encoded_keepalive(choice)
        time.sleep(0.10)

    expected = current_expected_sink_volumes(choice, filtered=filtered)
    if not any(expected):
        # Silence is intentional. A later volume increase will wake the sink.
        return

    # A one-percentage-point dip is practically inaudible and reproduces the
    # exact operation that was observed to wake v12 after an output switch.
    # Repeat it a few times because some IEC61937 receivers need more than one
    # fresh control/sample cycle before they lock.
    nudge = tuple(
        max(CHANNEL_VOLUME_MIN, value - 1) if value > 0 else 0
        for value in expected
    )

    for attempt in range(POST_SWITCH_WAKE_RETRIES):
        run(
            ["pactl", "suspend-sink", choice.sink_name, "0"],
            check=False,
        )

        if nudge != expected:
            _set_sink_channel_volumes(choice, nudge)
            time.sleep(POST_SWITCH_NUDGE_HOLD_SECONDS)

        _set_sink_channel_volumes(choice, expected)

        if attempt + 1 < POST_SWITCH_WAKE_RETRIES:
            time.sleep(POST_SWITCH_WAKE_DELAY_SECONDS)


def format_channel_volume(percent: int) -> str:
    return f"{int(percent)}%"


def wait_for_sink(
    sink_name: str,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        if sink_name in get_sinks():
            return True
        time.sleep(0.05)

    return False


def load_holding_sink(choice: Choice) -> None:
    if HOLDING_SINK in get_sinks():
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
            sink_properties_argument(HOLDING_DESCRIPTION),
        ],
        check=False,
    )

    module_id = result.stdout.strip()

    if result.returncode != 0 or not module_id.isdigit():
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Unknown PipeWire error."
        )
        raise RuntimeError(
            "Could not create the temporary holding sink: " + detail
        )

    if not wait_for_sink(HOLDING_SINK, SINK_TIMEOUT_SECONDS):
        raise RuntimeError(
            "The temporary holding sink did not appear in time."
        )


def set_default_sink(sink_name: str) -> None:
    run(["pactl", "set-default-sink", sink_name])


def move_all_streams_to_sink(sink_name: str) -> None:
    target_index = get_sinks().get(sink_name)

    if target_index is None:
        raise RuntimeError(
            f"Audio sink {sink_name} does not exist."
        )

    for input_id, current_sink_index in get_sink_inputs().items():
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
    set_default_sink(HOLDING_SINK)
    move_all_streams_to_sink(HOLDING_SINK)
    time.sleep(0.10)
    move_all_streams_to_sink(HOLDING_SINK)
    time.sleep(0.10)


def unload_output_modules() -> None:
    module_ids = get_module_ids_for_sink_names(OUTPUT_SINK_NAMES)

    for module_id in module_ids:
        run(
            ["pactl", "unload-module", module_id],
            check=False,
        )


def release_normal_hdmi_cards() -> None:
    run(
        ["pactl", "set-card-profile", CARD0, "off"],
        check=False,
    )
    run(
        ["pactl", "set-card-profile", CARD1, "off"],
        check=False,
    )


def enforce_encoder_output_visibility(choice: Choice) -> None:
    """Keep normal HDMI profiles hidden while a direct encoder sink is active."""
    if choice.sink_name not in get_sinks():
        return

    profiles = get_card_profiles()
    for card in (CARD0, CARD1):
        if profiles.get(card) not in (None, "off"):
            run(
                ["pactl", "set-card-profile", card, "off"],
                check=False,
            )


def output_modules_are_gone() -> bool:
    return not get_module_ids_for_sink_names(OUTPUT_SINK_NAMES)


def output_sinks_are_gone() -> bool:
    return not (set(get_sinks()) & OUTPUT_SINK_NAMES)


def normal_hdmi_sinks_are_gone() -> bool:
    for sink_name in get_sinks():
        if "107c701400" in sink_name or "107c706400" in sink_name:
            return False
    return True


def wait_until_hdmi_is_free() -> None:
    deadline = time.monotonic() + RELEASE_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if (
            output_modules_are_gone()
            and output_sinks_are_gone()
            and normal_hdmi_sinks_are_gone()
        ):
            time.sleep(0.20)
            return

        time.sleep(0.05)

    raise RuntimeError(
        "The previous HDMI output did not release completely in time."
    )


def fully_release_real_hdmi() -> None:
    stop_encoded_keepalive()
    unload_output_modules()
    release_normal_hdmi_cards()
    wait_until_hdmi_is_free()


def load_choice_sink(choice: Choice) -> None:
    last_error = "Unknown error."
    attempt_limit = 1 if choice.mode == MODE_PCM51 else LOAD_RETRIES

    for attempt in range(1, attempt_limit + 1):
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
                *(["mmap=0"] if not choice.mmap else []),
                "tsched=0",
                sink_properties_argument(
                    choice.description,
                    keep_open=(choice.mode in (MODE_AC3, MODE_DTS)),
                ),
            ],
            check=False,
        )

        module_id = result.stdout.strip()

        if result.returncode == 0 and module_id.isdigit():
            if wait_for_sink(choice.sink_name, SINK_TIMEOUT_SECONDS):
                apply_channel_volumes_to_sink(choice)
                return

            last_error = (
                "The module was created, but its audio sink did not appear."
            )
            run(
                ["pactl", "unload-module", module_id],
                check=False,
            )

        else:
            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"Attempt {attempt} failed without an error message."
            )

        unload_output_modules()
        release_normal_hdmi_cards()

        try:
            wait_until_hdmi_is_free()
        except RuntimeError:
            pass

        time.sleep(LOAD_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Could not open {choice.title} after "
        f"{attempt_limit} attempt"
        f"{'s' if attempt_limit != 1 else ''}: {last_error}"
    )


def encoded_surround_sink_names() -> set[str]:
    return {
        choice.sink_name
        for choice in CHOICES
        if choice.mode in (MODE_AC3, MODE_DTS)
    }


def find_encoded_keepalive_pids() -> list[int]:
    sink_arguments = {
        f"--device={sink_name}"
        for sink_name in encoded_surround_sink_names()
    }

    pids: list[int] = []

    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return pids

    for entry in proc_entries:
        if not entry.name.isdigit():
            continue

        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue

        args = [
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\0")
            if part
        ]

        if not args:
            continue

        if Path(args[0]).name != "paplay":
            continue

        if "--raw" not in args:
            continue

        if not any(argument in sink_arguments for argument in args):
            continue

        pids.append(int(entry.name))

    return pids


def stop_encoded_keepalive() -> None:
    pids = find_encoded_keepalive_pids()

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    if not pids:
        return

    deadline = time.monotonic() + 1.0

    while time.monotonic() < deadline:
        remaining = [
            pid
            for pid in pids
            if Path(f"/proc/{pid}").exists()
        ]

        if not remaining:
            return

        time.sleep(0.05)

    for pid in pids:
        if not Path(f"/proc/{pid}").exists():
            continue

        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def start_encoded_keepalive(choice: Choice) -> None:
    if choice.mode not in (MODE_AC3, MODE_DTS):
        return

    if shutil.which("paplay") is None:
        raise RuntimeError(
            "paplay is required for the Dolby/DTS keepalive stream."
        )

    stop_encoded_keepalive()

    zero = open(
        "/dev/zero",
        "rb",
        buffering=0,
    )

    try:
        process = subprocess.Popen(
            [
                "paplay",
                "--raw",
                f"--device={choice.sink_name}",
                "--rate=48000",
                "--channels=6",
                "--format=s16le",
                (
                    "--channel-map="
                    "front-left,front-right,rear-left,rear-right,"
                    "front-center,lfe"
                ),
            ],
            stdin=zero,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        zero.close()

    # Make sure paplay actually attached to the sink before application audio
    # is moved back. The detached process survives closing this GUI and also
    # survives the short-lived --restore process after login.
    time.sleep(0.15)

    if process.poll() is not None:
        raise RuntimeError(
            "Could not start the Dolby/DTS keepalive stream."
        )


def drain_holding_sink(target_sink: str) -> None:
    set_default_sink(target_sink)
    deadline = time.monotonic() + 2.0

    while time.monotonic() < deadline:
        holding_index = get_sinks().get(HOLDING_SINK)
        if holding_index is None:
            return

        holding_inputs = [
            input_id
            for input_id, sink_index in get_sink_inputs().items()
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


def remove_holding_sink() -> None:
    module_ids = get_module_ids_for_sink_names({HOLDING_SINK})

    for module_id in module_ids:
        run(
            ["pactl", "unload-module", module_id],
            check=False,
        )


def activate_choice(choice: Choice) -> tuple[bool, str | None]:
    with audio_operation_lock():
        return _activate_choice_locked(choice)


def _activate_choice_locked(choice: Choice) -> tuple[bool, str | None]:
    previous = get_current_choice()

    # A smart filter that still targets the old sink can keep links alive while
    # that sink is being removed. Disable it before releasing the old sink;
    # the new filter target will be selected after loading the new sink.
    try:
        disable_frequency_filters()
        time.sleep(0.08)
    except Exception:
        pass

    park_all_application_audio(choice)
    fully_release_real_hdmi()

    # IEC61937 receivers sometimes keep the previous Dolby/DTS lock briefly.
    # Give the optical/HDMI path a clean carrier-free gap before opening the new
    # encoder. This mirrors the manual reconnect that reliably recovers the SONY.
    if (
        choice.mode in (MODE_AC3, MODE_DTS)
        or (previous is not None and previous.mode in (MODE_AC3, MODE_DTS))
    ):
        time.sleep(RECEIVER_RELEASE_SECONDS)

    load_choice_sink(choice)

    # Build the selected DSP path before any audio or encoded carrier is moved
    # to the new sink. On failure the previous direct volume path still works.
    filtered, filter_error = apply_audio_controls(
        choice, allow_restart=True
    )

    if choice.mode in (MODE_AC3, MODE_DTS):
        start_encoded_keepalive(choice)
        # Let the receiver lock to the fresh AC-3/DTS carrier before application
        # audio leaves the holding sink.
        time.sleep(KEEPALIVE_LEAD_SECONDS)

    drain_holding_sink(choice.sink_name)

    time.sleep(0.10)
    remove_holding_sink()
    enforce_encoder_output_visibility(choice)
    return filtered, filter_error


def apply_choice(choice: Choice) -> str:
    with audio_operation_lock():
        return _apply_choice_locked(choice)


def _apply_choice_locked(choice: Choice) -> str:
    previous = get_current_choice()

    try:
        filtered, filter_error = activate_choice(choice)

        enforce_encoder_output_visibility(choice)

        # v12 sometimes stayed silent until a Volume / channel slider was moved.
        # Perform that wake-up automatically after the final graph is in place.
        time.sleep(0.10)
        wake_selected_output(choice, filtered=filtered)
        # Only commit the selected output after its full switch has succeeded.
        save_audio_state(choice)
        if filter_error:
            return f"Active: {choice.title} · DSP unavailable: {filter_error}"
        return f"Active: {choice.title}"

    except Exception as original_error:
        try:
            park_all_application_audio(previous or choice)
        except Exception:
            pass

        try:
            fully_release_real_hdmi()
        except Exception:
            pass

        if previous is not None:
            try:
                load_choice_sink(previous)
                apply_audio_controls(previous, allow_restart=True)

                if previous.mode in (MODE_AC3, MODE_DTS):
                    start_encoded_keepalive(
                        previous
                    )
                    time.sleep(
                        KEEPALIVE_LEAD_SECONDS
                    )

                set_default_sink(previous.sink_name)
                drain_holding_sink(previous.sink_name)
                remove_holding_sink()
            except Exception:
                pass

        raise RuntimeError(str(original_error)) from original_error


class FrequencyBalanceDialog(Adw.Dialog):
    def __init__(
        self,
        parent: "AudioWindow",
        channel_key: str,
    ):
        super().__init__()

        self.parent_window = parent
        self.channel_key = channel_key
        self.original_values = list(FREQUENCY_BALANCE[channel_key])
        self.working_values = list(self.original_values)
        self.live_source_id = 0
        self.finished = False

        self.set_title(
            f"{CHANNEL_TITLES[channel_key]} – Frequency Balance"
        )
        self.set_content_width(760)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        header = Adw.HeaderBar()
        # Native libadwaita dialog/header, but the requested actions are only
        # Default / Cancel / OK, so no extra title-bar close button is shown.
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        toolbar.set_content(body)

        bands_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        bands_box.set_hexpand(True)
        bands_box.set_vexpand(True)
        body.append(bands_box)

        self.scales: list[Gtk.Scale] = []
        self.value_labels: list[Gtk.Label] = []

        for index, (range_label, friendly_name, _, _, _) in enumerate(
            FREQUENCY_BANDS
        ):
            column = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=5,
            )
            column.set_hexpand(True)
            column.set_halign(Gtk.Align.FILL)
            bands_box.append(column)

            max_label = Gtk.Label(label="100%")
            max_label.add_css_class("dim-label")
            column.append(max_label)

            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.VERTICAL,
                FREQUENCY_MIN,
                FREQUENCY_MAX,
                1,
            )
            scale.set_draw_value(False)
            scale.set_digits(0)
            scale.set_round_digits(0)
            scale.set_inverted(True)
            scale.set_vexpand(True)
            scale.set_size_request(50, 250)
            scale.set_value(self.working_values[index])
            scale.set_tooltip_text(
                f"{range_label} · {friendly_name}"
            )
            scale.connect(
                "value-changed",
                self.on_band_changed,
                index,
            )
            column.append(scale)

            min_label = Gtk.Label(label="0%")
            min_label.add_css_class("dim-label")
            column.append(min_label)

            range_widget = Gtk.Label(label=range_label)
            range_widget.set_wrap(True)
            range_widget.set_justify(Gtk.Justification.CENTER)
            column.append(range_widget)

            name_widget = Gtk.Label(label=friendly_name)
            name_widget.set_wrap(True)
            name_widget.set_justify(Gtk.Justification.CENTER)
            name_widget.add_css_class("dim-label")
            column.append(name_widget)

            value_widget = Gtk.Label(
                label=f"{self.working_values[index]}%"
            )
            value_widget.set_markup(
                f"<b>{self.working_values[index]}%</b>"
            )
            column.append(value_widget)

            self.scales.append(scale)
            self.value_labels.append(value_widget)

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        body.append(separator)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        actions.set_hexpand(True)
        body.append(actions)

        default_button = Gtk.Button(label="Default")
        default_button.set_tooltip_text("Set all bands to 100%")
        default_button.connect("clicked", self.on_default_clicked)
        actions.append(default_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", self.on_cancel_clicked)
        actions.append(cancel_button)

        ok_button = Gtk.Button(label="OK")
        ok_button.add_css_class("suggested-action")
        ok_button.connect("clicked", self.on_ok_clicked)
        actions.append(ok_button)
        self.set_default_widget(ok_button)

        # Escape / another native close gesture behaves exactly like Cancel.
        self.connect("closed", self.on_dialog_closed)

    def on_band_changed(
        self,
        scale: Gtk.Scale,
        index: int,
    ) -> None:
        value = max(
            FREQUENCY_MIN,
            min(FREQUENCY_MAX, int(round(scale.get_value()))),
        )
        self.working_values[index] = value
        self.value_labels[index].set_markup(f"<b>{value}%</b>")
        self.schedule_live_apply()

    def schedule_live_apply(self) -> None:
        if self.live_source_id:
            GLib.source_remove(self.live_source_id)
            self.live_source_id = 0

        self.live_source_id = GLib.timeout_add(
            60,
            self.apply_live,
        )

    def apply_live(self) -> bool:
        self.live_source_id = 0
        self.parent_window.queue_frequency_preview(
            self.channel_key, self.working_values
        )
        return False

    def on_default_clicked(self, button: Gtk.Button) -> None:
        for index, scale in enumerate(self.scales):
            self.working_values[index] = FREQUENCY_DEFAULT
            scale.set_value(FREQUENCY_DEFAULT)
            self.value_labels[index].set_markup(
                f"<b>{FREQUENCY_DEFAULT}%</b>"
            )
        self.schedule_live_apply()

    def restore_original_live(self) -> None:
        self.parent_window.queue_frequency_preview(self.channel_key, None)

    def on_cancel_clicked(self, button: Gtk.Button) -> None:
        if self.live_source_id:
            GLib.source_remove(self.live_source_id)
            self.live_source_id = 0
        self.finished = True
        self.restore_original_live()
        self.close()

    def on_ok_clicked(self, button: Gtk.Button) -> None:
        if self.live_source_id:
            GLib.source_remove(self.live_source_id)
            self.live_source_id = 0

        FREQUENCY_BALANCE[self.channel_key] = list(self.working_values)
        self.parent_window.queue_frequency_save(
            self.channel_key, self.working_values
        )
        self.finished = True
        self.close()

    def on_dialog_closed(self, dialog: Adw.Dialog) -> None:
        if self.finished:
            return
        # Native Escape/dismiss is Cancel, never an implicit save.
        if self.live_source_id:
            GLib.source_remove(self.live_source_id)
            self.live_source_id = 0
        self.restore_original_live()


class AudioWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)

        self.set_title("HDMI Audio Encoder")
        self.set_icon_name("audio-card")
        self.set_default_size(880, 730)
        self.set_resizable(False)

        self.switch_locked = False
        self.switch_started_at = 0.0
        self.volume_commit_source_id = 0
        self.volume_worker_active = False
        self.pending_volume_changes: dict[str, int] = {}
        self.pending_frequency_saves: dict[str, list[int]] = {}
        self.frequency_preview_override: dict[str, list[int]] | None = None
        self.control_update_requested = False
        self.last_local_volume_change_at = 0.0
        self.syncing_volume_ui = False
        self.hdmi_connected: dict[str, bool] = {}
        self.pcm_detection_labels: dict[str, Gtk.Label] = {}
        self.hdmi_uevent_socket: socket.socket | None = None
        self.hdmi_uevent_source_id = 0
        self.hdmi_rescan_source_id = 0
        self.hdmi_fallback_source_id = 0

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        toolbar.set_content(content)

        left_wrapper = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        left_wrapper.set_hexpand(True)
        left_wrapper.set_size_request(399, -1)
        content.append(left_wrapper)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        left_wrapper.append(page)

        group = Adw.PreferencesGroup(
            title="Audio output",
            description="Choose one HDMI port and one audio mode.",
        )
        page.add(group)

        self.status = Adw.ActionRow(
            title="Status",
            subtitle="Reading audio settings…",
        )
        self.status.add_prefix(
            Gtk.Image.new_from_icon_name("audio-card-symbolic")
        )
        group.add(self.status)

        self.rows: dict[
            str,
            tuple[Adw.ActionRow, Gtk.CheckButton],
        ] = {}

        first_button: Gtk.CheckButton | None = None

        for choice in CHOICES:
            row = Adw.ActionRow(
                title=choice.title,
                subtitle=choice.subtitle,
            )
            row.set_activatable(True)

            button = Gtk.CheckButton()
            button.set_valign(Gtk.Align.CENTER)
            button.set_can_focus(False)
            button.set_can_target(False)

            if first_button is None:
                first_button = button
            else:
                button.set_group(first_button)

            if choice.mode == MODE_PCM51:
                detection_label = Gtk.Label()
                detection_label.set_markup("<i>Not detected</i>")
                detection_label.add_css_class("caption")
                detection_label.add_css_class("dim-label")
                row.add_suffix(detection_label)
                self.pcm_detection_labels[choice.key] = detection_label

            row.add_suffix(button)
            row.connect("activated", self.on_row_activated, choice)
            group.add(row)
            self.rows[choice.key] = (row, button)

        info_footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        info_footer.set_margin_top(8)
        info_footer.set_margin_bottom(16)
        info_footer.set_margin_start(18)
        info_footer.set_margin_end(18)
        left_wrapper.append(info_footer)

        info_icon = Gtk.Image.new_from_icon_name(
            "dialog-information-symbolic"
        )
        info_icon.set_valign(Gtk.Align.START)
        info_icon.set_margin_top(2)
        info_footer.append(info_icon)

        info_text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        info_text.set_hexpand(True)
        info_footer.append(info_text)

        info_title = Gtk.Label()
        info_title.set_markup("<b>PCM / Dolby Digital / DTS 5.1</b>")
        info_title.set_xalign(0.0)
        info_title.set_wrap(True)
        info_text.append(info_title)

        info_body = Gtk.Label()
        info_body.set_markup(
            "PCM 5.1 requires supported HDMI hardware. "
            "Dolby Digital / DTS require a decoder. "
            "Unsupported modes may produce no audio or digital noise. "
            f"Version&#160;{APP_VERSION}&#160;"
            f"<i>{APP_VERSION_DATE.replace(' ', '&#160;')}</i>"
        )
        info_body.set_xalign(0.0)
        info_body.set_wrap(True)
        info_body.add_css_class("dim-label")
        info_body.add_css_class("caption")
        info_text.append(info_body)

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL
        )
        content.append(separator)

        right_wrapper = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        right_wrapper.set_hexpand(True)
        right_wrapper.set_size_request(440, -1)
        right_wrapper.set_margin_top(24)
        right_wrapper.set_margin_bottom(24)
        right_wrapper.set_margin_start(18)
        right_wrapper.set_margin_end(18)
        content.append(right_wrapper)

        heading = Gtk.Label(label="Channel volume")
        heading.set_xalign(0.0)
        heading.set_markup("<b>Channel volume</b>")
        right_wrapper.append(heading)

        self.volume_scales: dict[str, Gtk.Scale] = {}
        self.volume_value_labels: dict[str, Gtk.Label] = {}
        self.frequency_buttons: dict[str, Gtk.Button] = {}

        for index, (setting_key, setting_title) in enumerate(VOLUME_PANEL):
            row_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
            )
            row_box.set_hexpand(True)
            row_box.set_vexpand(True)

            title_label = Gtk.Label()
            title_label.set_markup(f"<b>{setting_title}</b>")
            title_label.set_xalign(1.0)
            title_label.set_size_request(100, -1)
            title_label.set_valign(Gtk.Align.CENTER)
            row_box.append(title_label)

            scale_maximum = (
                MASTER_VOLUME_MAX
                if setting_key == "volume"
                else CHANNEL_VOLUME_MAX
            )
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL,
                CHANNEL_VOLUME_MIN,
                scale_maximum,
                1,
            )
            scale.set_draw_value(False)
            scale.set_round_digits(0)
            scale.set_hexpand(True)
            scale.set_size_request(150, -1)
            scale.set_valign(Gtk.Align.CENTER)

            if setting_key == "volume":
                current_value = MASTER_VOLUME
            else:
                current_value = CHANNEL_VOLUMES[setting_key]

            scale.set_value(current_value)

            # No GtkScale marks here: GTK snaps to nearby marks, which made
            # the speaker sliders jump around 0% and 100%. The range remains a
            # uniform 1% step across the complete slider.

            row_box.append(scale)

            value_label = Gtk.Label(
                label=format_channel_volume(current_value)
            )
            value_label.set_xalign(1.0)
            value_label.set_size_request(58, -1)
            value_label.set_valign(Gtk.Align.CENTER)
            row_box.append(value_label)

            if setting_key != "volume":
                edit_button = Gtk.Button.new_from_icon_name(
                    "document-edit-symbolic"
                )
                edit_button.add_css_class("flat")
                edit_button.set_valign(Gtk.Align.CENTER)
                edit_button.set_size_request(36, 36)
                edit_button.set_tooltip_text(
                    f"Edit {setting_title} Frequency Balance"
                )
                edit_button.connect(
                    "clicked",
                    self.on_frequency_edit_clicked,
                    setting_key,
                )
                row_box.append(edit_button)
                self.frequency_buttons[setting_key] = edit_button
            else:
                # Reserve the same final column as the six edit buttons so the
                # master slider and all six speaker sliders have identical width.
                edit_placeholder = Gtk.Box()
                edit_placeholder.set_size_request(36, 36)
                edit_placeholder.set_can_target(False)
                row_box.append(edit_placeholder)

            scale.connect(
                "value-changed",
                self.on_volume_changed,
                setting_key,
                value_label,
            )

            self.volume_scales[setting_key] = scale
            self.volume_value_labels[setting_key] = value_label
            right_wrapper.append(row_box)

        self.start_hdmi_hotplug_monitor()
        self.refresh_hdmi_availability()
        self.schedule_hdmi_rescan()
        self.connect("close-request", self.stop_hdmi_hotplug_monitor)
        GLib.idle_add(self.refresh)
        GLib.timeout_add(
            400,
            self.sync_volume_from_system,
        )

    def set_rows_sensitive(self, sensitive: bool) -> None:
        for choice in CHOICES:
            row, _ = self.rows[choice.key]
            row.set_sensitive(
                sensitive and self.hdmi_connected.get(choice.card, False)
            )

    def refresh_hdmi_availability(self) -> bool:
        for card, choice_key in (
            (CARD0, "hdmi0-stereo"),
            (CARD1, "hdmi1-stereo"),
        ):
            self.hdmi_connected[card] = hdmi_port_connected(
                CHOICE_BY_KEY[choice_key]
            )

        self.set_rows_sensitive(not self.switch_locked)

        for choice_key in ("hdmi0-pcm51", "hdmi1-pcm51"):
            choice = CHOICE_BY_KEY[choice_key]
            detected = (
                self.hdmi_connected[choice.card]
                and hdmi_pcm51_capability(choice) is True
            )
            self.pcm_detection_labels[choice_key].set_markup(
                "<i>Detected</i>" if detected else "<i>Not detected</i>"
            )

        return False

    def start_hdmi_hotplug_monitor(self) -> None:
        monitor: socket.socket | None = None
        try:
            # The kernel broadcasts DRM hotplug events on this netlink group.
            monitor = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)
            monitor.bind((os.getpid(), 1))
            monitor.setblocking(False)
            self.hdmi_uevent_source_id = GLib.io_add_watch(
                monitor.fileno(),
                GLib.PRIORITY_DEFAULT,
                GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self.on_hdmi_uevent,
            )
            self.hdmi_uevent_socket = monitor
        except (OSError, RuntimeError, TypeError):
            if monitor is not None:
                monitor.close()
            # Only use a timer if this system denies the hotplug socket.
            self.hdmi_fallback_source_id = GLib.timeout_add_seconds(
                1, self.refresh_hdmi_availability_for_fallback
            )

    def refresh_hdmi_availability_for_fallback(self) -> bool:
        self.refresh_hdmi_availability()
        return True

    def schedule_hdmi_rescan(self) -> None:
        # HDMI audio capabilities can arrive shortly after the video link.
        if self.hdmi_rescan_source_id:
            GLib.source_remove(self.hdmi_rescan_source_id)
        self.hdmi_rescan_source_id = GLib.timeout_add(
            900, self.rescan_hdmi_after_hotplug
        )

    def on_hdmi_uevent(self, fd: int, condition: GLib.IOCondition) -> bool:
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            self.hdmi_uevent_source_id = 0
            self.hdmi_uevent_socket.close()
            self.hdmi_uevent_socket = None
            self.hdmi_fallback_source_id = GLib.timeout_add_seconds(
                1, self.refresh_hdmi_availability_for_fallback
            )
            return False

        try:
            while True:
                message = self.hdmi_uevent_socket.recv(8192)
                properties = set(message.split(b"\0"))
                if (
                    b"SUBSYSTEM=drm" in properties
                    and b"HOTPLUG=1" in properties
                ):
                    self.refresh_hdmi_availability()
                    self.schedule_hdmi_rescan()
        except BlockingIOError:
            pass
        except OSError:
            self.hdmi_uevent_source_id = 0
            self.hdmi_uevent_socket.close()
            self.hdmi_uevent_socket = None
            self.hdmi_fallback_source_id = GLib.timeout_add_seconds(
                1, self.refresh_hdmi_availability_for_fallback
            )
            return False

        return True

    def rescan_hdmi_after_hotplug(self) -> bool:
        self.hdmi_rescan_source_id = 0
        return self.refresh_hdmi_availability()

    def stop_hdmi_hotplug_monitor(self, window: Adw.ApplicationWindow) -> bool:
        # A final drag can still be waiting for the debounce timer. Persist
        # that snapshot before GTK tears down the daemon worker.
        if self.pending_volume_changes or self.pending_frequency_saves:
            try:
                save_audio_state(
                    master_volume=self.pending_volume_changes.get("volume"),
                    channel_volumes={
                        key: value for key, value in self.pending_volume_changes.items()
                        if key != "volume"
                    },
                    frequency_balance=self.pending_frequency_saves,
                )
            except Exception:
                pass
        for source_id in (
            self.hdmi_uevent_source_id,
            self.hdmi_rescan_source_id,
            self.hdmi_fallback_source_id,
        ):
            if source_id:
                GLib.source_remove(source_id)

        if self.hdmi_uevent_socket is not None:
            self.hdmi_uevent_socket.close()
            self.hdmi_uevent_socket = None

        return False

    def set_channel_controls_sensitive(
        self,
        sensitive: bool,
    ) -> None:
        for scale in self.volume_scales.values():
            scale.set_sensitive(sensitive)
        for button in self.frequency_buttons.values():
            button.set_sensitive(sensitive)

    def on_frequency_edit_clicked(
        self,
        button: Gtk.Button,
        channel_key: str,
    ) -> None:
        if self.switch_locked:
            return
        dialog = FrequencyBalanceDialog(self, channel_key)
        dialog.present(self)


    def on_volume_changed(
        self,
        scale: Gtk.Scale,
        setting_key: str,
        value_label: Gtk.Label,
    ) -> None:
        global MASTER_VOLUME

        if self.syncing_volume_ui:
            return

        value = int(round(scale.get_value()))

        if setting_key == "volume":
            value = max(
                MASTER_VOLUME_MIN,
                min(MASTER_VOLUME_MAX, value),
            )
            MASTER_VOLUME = value
        else:
            value = max(
                CHANNEL_VOLUME_MIN,
                min(CHANNEL_VOLUME_MAX, value),
            )
            CHANNEL_VOLUMES[setting_key] = value

        value_label.set_label(
            format_channel_volume(value)
        )

        self.pending_volume_changes[setting_key] = value
        self.last_local_volume_change_at = time.monotonic()
        self.schedule_control_commit()

    def queue_frequency_preview(
        self, channel_key: str, values: list[int] | None
    ) -> None:
        self.frequency_preview_override = (
            {channel_key: list(values)} if values is not None else None
        )
        self.last_local_volume_change_at = time.monotonic()
        self.schedule_control_commit()

    def queue_frequency_save(
        self, channel_key: str, values: list[int]
    ) -> None:
        self.pending_frequency_saves[channel_key] = list(values)
        self.frequency_preview_override = None
        self.last_local_volume_change_at = time.monotonic()
        self.schedule_control_commit()

    def schedule_control_commit(self) -> None:
        self.control_update_requested = True
        # Throttle during a continuous drag; restarting this timer for every
        # event would postpone the audible update until the mouse is released.
        if self.volume_commit_source_id or self.volume_worker_active:
            return
        self.volume_commit_source_id = GLib.timeout_add(
            75,
            self.commit_channel_volumes,
        )

    def commit_channel_volumes(self) -> bool:
        self.volume_commit_source_id = 0
        if self.volume_worker_active or not self.control_update_requested:
            return False

        changes = self.pending_volume_changes
        frequency_saves = self.pending_frequency_saves
        override = self.frequency_preview_override
        self.pending_volume_changes = {}
        self.pending_frequency_saves = {}
        self.control_update_requested = False
        self.volume_worker_active = True

        def worker() -> None:
            error: str | None = None
            try:
                if changes or frequency_saves:
                    save_audio_state(
                        master_volume=changes.get("volume"),
                        channel_volumes={
                            key: value for key, value in changes.items()
                            if key != "volume"
                        },
                        frequency_balance=frequency_saves,
                    )
                current = get_current_choice()
                if current is not None and current.sink_name in get_sinks():
                    _, error = apply_live_audio_controls(
                        current,
                        override=override,
                        master_changed="volume" in changes,
                        speaker_changed=(
                            any(key != "volume" for key in changes)
                            or bool(frequency_saves) or override is not None
                        ),
                    )
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(self.control_commit_done, error)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def control_commit_done(self, error: str | None) -> bool:
        self.volume_worker_active = False
        if error:
            self.status.set_subtitle("Volume/Frequency: " + error)
        if self.control_update_requested:
            self.schedule_control_commit()
        return False

    def sync_volume_from_system(self) -> bool:
        """The restore monitor publishes GNOME changes in the state file.

        Reading that file is cheap; pactl and pw-dump must never run on the
        GTK timer or move a slider back while the user is still editing it.
        """
        global MASTER_VOLUME

        if (
            self.switch_locked or self.volume_worker_active
            or self.control_update_requested
            or time.monotonic() - self.last_local_volume_change_at < 1.25
        ):
            return True

        try:
            _, saved_volumes, saved_master = read_audio_state()
            MASTER_VOLUME = saved_master
            CHANNEL_VOLUMES.clear()
            CHANNEL_VOLUMES.update(saved_volumes)
            self.syncing_volume_ui = True
            try:
                master_scale = self.volume_scales["volume"]
                if int(round(master_scale.get_value())) != MASTER_VOLUME:
                    master_scale.set_value(MASTER_VOLUME)
                self.volume_value_labels["volume"].set_label(
                    format_channel_volume(MASTER_VOLUME)
                )
                for key in CHANNEL_KEYS:
                    scale = self.volume_scales[key]
                    value = CHANNEL_VOLUMES[key]
                    if int(round(scale.get_value())) != value:
                        scale.set_value(value)
                    self.volume_value_labels[key].set_label(
                        format_channel_volume(value)
                    )
            finally:
                self.syncing_volume_ui = False
        except Exception:
            pass
        return True



    def set_visual_choice(self, choice: Choice) -> None:
        _, button = self.rows[choice.key]
        button.set_active(True)

    def refresh(self) -> bool:
        try:
            current = get_current_choice()

            if current is None:
                self.status.set_subtitle(
                    "No active RPi HDMI Audio output found."
                )
                return False

            if frequency_filter_is_active(current) or not dsp_requested(current):
                self.status.set_subtitle(f"Active: {current.title}")
            else:
                self.status.set_subtitle(
                    f"Active: {current.title} · DSP unavailable; "
                    "direct channel volume in use"
                )
            self.set_visual_choice(current)

        except Exception as exc:
            self.status.set_subtitle(f"Error: {exc}")

        return False

    def on_row_activated(
        self,
        row: Adw.ActionRow,
        choice: Choice,
    ) -> None:
        if self.switch_locked:
            return

        if not hdmi_port_connected(choice):
            self.refresh_hdmi_availability()
            return

        current = get_current_choice()
        restarting = current is not None and current.key == choice.key

        self.set_visual_choice(choice)
        self.switch_locked = True
        self.switch_started_at = time.monotonic()
        self.set_rows_sensitive(False)
        self.set_channel_controls_sensitive(False)

        if restarting:
            self.status.set_subtitle(f"Restarting {choice.title}…")
        else:
            self.status.set_subtitle(f"Switching to {choice.title}…")

        def worker() -> None:
            try:
                message = apply_choice(choice)
                GLib.idle_add(self.switch_done, message, None)
            except Exception as exc:
                GLib.idle_add(self.switch_done, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def switch_done(
        self,
        message: str | None,
        error: str | None,
    ) -> bool:
        if error:
            self.status.set_subtitle(f"Error: {error}")
        else:
            self.status.set_subtitle(message or "Audio output changed.")

        self.refresh()

        elapsed = time.monotonic() - self.switch_started_at
        remaining = max(0.0, SWITCH_LOCK_SECONDS - elapsed)

        if remaining <= 0:
            self.unlock_switching()
        else:
            GLib.timeout_add(
                max(1, int(remaining * 1000)),
                self.unlock_switching,
            )

        return False

    def unlock_switching(self) -> bool:
        self.switch_locked = False
        self.set_rows_sensitive(True)
        self.set_channel_controls_sensitive(True)
        self.refresh()
        return False



def audio_state_signature() -> tuple[
    str | None,
    int,
    tuple[int, ...],
    tuple[tuple[int, ...], ...],
]:
    choice, volumes, master_volume = read_audio_state()
    frequencies = load_frequency_balance_state()

    return (
        choice.key if choice is not None else None,
        master_volume,
        tuple(
            volumes[key]
            for key in CHANNEL_KEYS
        ),
        tuple(tuple(frequencies[key]) for key in CHANNEL_KEYS),
    )


def find_volume_monitor_pids() -> list[int]:
    pids: list[int] = []

    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return pids

    for entry in proc_entries:
        if not entry.name.isdigit():
            continue

        pid = int(entry.name)

        if pid == os.getpid():
            continue

        try:
            raw = (entry / "cmdline").read_bytes()
        except (
            FileNotFoundError,
            PermissionError,
            ProcessLookupError,
            OSError,
        ):
            continue

        args = [
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\0")
            if part
        ]

        if not args:
            continue

        if "--restore-monitor" not in args:
            continue

        if not any(
            "rpi-hdmi-audio" in argument
            for argument in args
        ):
            continue

        pids.append(pid)

    return pids


def stop_volume_monitor() -> None:
    for pid in find_volume_monitor_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def monitor_volume_forever() -> int:
    global MASTER_VOLUME

    last_state = audio_state_signature()
    last_choice_key: str | None = None
    last_actual: tuple[int, ...] | None = None
    last_dsp_check = time.monotonic()
    last_dsp_repair_nodes: tuple[tuple[str, int], ...] | None = None

    while True:
        try:
            current = get_current_choice()

            if current is None:
                last_choice_key = None
                last_actual = None
                last_dsp_repair_nodes = None
                last_state = audio_state_signature()
                time.sleep(0.50)
                continue

            # While our direct sink owns audio, the ordinary ALSA HDMI profiles
            # stay off so GNOME Quick Settings only exposes the selected encoder
            # output. They are restored by the remover/fallback path.
            enforce_encoder_output_visibility(current)

            actual = get_sink_channel_volumes(current)

            if actual is None:
                time.sleep(0.50)
                continue

            state_now = audio_state_signature()

            if current.key != last_choice_key:
                reload_volume_state_from_disk()
                filtered, _ = apply_audio_controls(current, allow_restart=True)
                last_choice_key = current.key
                last_state = state_now
                last_actual = (
                    get_sink_channel_volumes(current)
                    or current_expected_sink_volumes(current, filtered=filtered)
                )
                last_dsp_check = time.monotonic()
                last_dsp_repair_nodes = tuple(
                    sorted(_frequency_node_ids().items())
                )
                time.sleep(0.50)
                continue

            if state_now != last_state:
                # The GUI changed master, speaker gain or Frequency Balance.
                reload_volume_state_from_disk()
                filtered, _ = apply_audio_controls(current)

                last_state = audio_state_signature()
                last_actual = (
                    get_sink_channel_volumes(current)
                    or current_expected_sink_volumes(current, filtered=filtered)
                )

                time.sleep(0.50)
                continue

            if (
                last_actual is not None
                and not volumes_are_close(actual, last_actual)
            ):
                # PipeWire changed without our state changing. GNOME master
                # volume is allowed; Balance/Fade/Subwoofer is not authoritative.
                reload_volume_state_from_disk()

                new_master = derive_external_master_volume(
                    current,
                    actual,
                    filtered=frequency_filter_is_active(current),
                )

                if new_master is not None and new_master != MASTER_VOLUME:
                    MASTER_VOLUME = new_master
                    save_audio_state(master_volume=new_master)

                # Restore a uniform physical master after an asymmetric GNOME
                # Balance edit; speaker trims live inside the DSP graph.
                filtered = frequency_filter_is_active(current)
                apply_channel_volumes_to_sink(current, filtered=filtered)

                last_state = audio_state_signature()
                last_actual = (
                    get_sink_channel_volumes(current)
                    or current_expected_sink_volumes(current, filtered=filtered)
                )
            else:
                last_actual = actual

            # A restarted service creates new node IDs. Repair its graph once,
            # without repeatedly relinking a running encoded stream if
            # metadata is unavailable to the monitor.
            if time.monotonic() - last_dsp_check >= 10.0:
                last_dsp_check = time.monotonic()
                nodes = tuple(sorted(_frequency_node_ids().items()))
                if (
                    dsp_requested(current)
                    and
                    not frequency_filter_is_active(current)
                    and (not nodes or nodes != last_dsp_repair_nodes)
                ):
                    reload_volume_state_from_disk()
                    filtered, _ = apply_audio_controls(
                        current, allow_restart=True
                    )
                    last_dsp_repair_nodes = tuple(
                        sorted(_frequency_node_ids().items())
                    )
                    last_actual = (
                        get_sink_channel_volumes(current)
                        or current_expected_sink_volumes(current, filtered=filtered)
                    )
                    last_state = audio_state_signature()

            time.sleep(0.50)

        except KeyboardInterrupt:
            return 0

        except Exception:
            # Survive temporary PipeWire / ALSA changes.
            time.sleep(0.75)


def restore_and_monitor() -> int:
    stop_volume_monitor()

    result = restore_saved_choice()

    # Keep monitoring even if restore temporarily fell back to normal stereo.
    monitor_volume_forever()

    return result


def wait_for_audio_stack(
    required_card: str,
    timeout_seconds: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        if run(["pactl", "info"], check=False).returncode == 0:
            cards = run(
                ["pactl", "list", "cards", "short"],
                check=False,
            ).stdout

            if required_card in cards:
                return True

        time.sleep(0.20)

    return False


def restore_normal_hdmi0_stereo() -> int:
    try:
        stop_encoded_keepalive()
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
    with audio_operation_lock():
        return _restore_saved_choice_locked()


def _restore_saved_choice_locked() -> int:
    choice = load_choice_state() or CHOICE_BY_KEY["hdmi0-stereo"]
    reload_volume_state_from_disk()

    if not wait_for_audio_stack(
        choice.card,
        timeout_seconds=30.0,
    ):
        return 1

    # GNOME autostart can begin while PipeWire, WirePlumber and ALSA are still
    # settling. Retry the complete direct-sink restore instead of immediately
    # falling back to normal stereo after the first failed ALSA open.
    for attempt in range(5):

        try:
            write_frequency_filter_config()
            filtered, _ = activate_choice(choice)
            enforce_encoder_output_visibility(choice)
            time.sleep(0.10)
            wake_selected_output(choice, filtered=filtered)
            save_audio_state(choice)

            return 0

        except Exception:

            try:
                remove_holding_sink()
            except Exception:
                pass

            try:
                unload_output_modules()
            except Exception:
                pass

            if attempt < 4:
                time.sleep(1.0)

    return restore_normal_hdmi0_stereo()


class AudioApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self) -> None:
        win = self.props.active_window

        if win is None:
            win = AudioWindow(self)

        win.present()


def main() -> int:
    if "--stop-monitor" in sys.argv[1:]:
        stop_volume_monitor()
        return 0

    if "--stop-keepalive" in sys.argv[1:]:
        stop_encoded_keepalive()
        return 0

    if "--write-filter-config" in sys.argv[1:]:
        write_frequency_filter_config()
        return 0

    if "--disable-frequency-filter" in sys.argv[1:]:
        disable_frequency_filters()
        return 0

    if "--restore-monitor" in sys.argv[1:]:
        return restore_and_monitor()

    if "--restore" in sys.argv[1:]:
        return restore_saved_choice()

    app = AudioApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
