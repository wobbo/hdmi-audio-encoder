# RPi HDMI Audio

HDMI audio output manager for Raspberry Pi 5 / Raspberry Pi 500+ running Debian 13 GNOME.

RPi HDMI Audio adds selectable **Stereo 2.0, PCM 5.1, Dolby Digital 5.1 and DTS 5.1** output modes for both Raspberry Pi HDMI ports.

It also provides independent speaker levels, per-speaker frequency balance and GNOME integration.

[Download: version 16](https://github.com/wobbo/hdmi-audio-encoder/releases/tag/v16)

<img height="400" alt="RPi HDMI Audio" src="https://github.com/user-attachments/assets/120d9bf2-e0cd-4798-a857-e54f563dd929" /> <img height="200" alt="GNOME Sound outputs" src="https://github.com/user-attachments/assets/59ff0c9f-68a2-4168-b561-0844bf366100" />

## Audio outputs

Eight output modes are available:

- HDMI 0 - Stereo 2.0
- HDMI 0 - PCM 5.1
- HDMI 0 - Dolby Digital 5.1
- HDMI 0 - DTS 5.1
- HDMI 1 - Stereo 2.0
- HDMI 1 - PCM 5.1
- HDMI 1 - Dolby Digital 5.1
- HDMI 1 - DTS 5.1

<img height="300" alt="ChatGPT Image 11 sep 2026, 17_06_59" src="https://github.com/user-attachments/assets/800da03a-f9a3-4516-95fe-243715e95761" /><img height="300" alt="ChatGPT Image 11 sep 2026, 17_09_48" src="https://github.com/user-attachments/assets/2f007abe-a36d-4bd1-ab21-c5504fb35bf4" />


### Stereo 2.0

Normal uncompressed two-channel HDMI audio.

### PCM 5.1

Uncompressed six-channel PCM audio over HDMI.

PCM 5.1 requires the connected HDMI device to support multichannel PCM. Not every monitor, TV, receiver or HDMI audio extractor supports it.

### Dolby Digital 5.1

Six-channel audio is encoded to Dolby Digital (AC-3) in real time.

This makes it possible to send 5.1 audio to compatible receivers and digital audio equipment, including setups using optical S/PDIF after HDMI audio extraction.

### DTS 5.1

Six-channel audio can also be encoded to DTS in real time for compatible DTS decoders.

## Volume and speaker control

The application has a normal **Master Volume from 0-100%**, matching the normal GNOME volume range.

Each individual 5.1 speaker can be adjusted independently from **0-150%**:

- Front Left
- Front Right
- Center
- Rear Left
- Rear Right
- Subwoofer / LFE

The six speaker levels are stored separately from the master volume.

This allows individual channels to be corrected or boosted without making the main volume control confusing.

## Frequency Balance

Each speaker also has its own 8-band Frequency Balance control:

- Deep Bass
- Bass
- Upper Bass
- Low Mid
- Mid
- Upper Mid
- Presence
- High

Frequency Balance can be adjusted independently for every speaker.

The settings are shared between the different audio modes, so the same speaker correction can be used with Stereo, PCM, Dolby Digital and DTS.

Leaving all bands at 100% bypasses the frequency adjustment.

## Switching outputs

RPi HDMI Audio can switch between HDMI ports and audio modes while applications are already playing audio.

Active browser and media streams are temporarily parked while the HDMI output is changed and are then moved to the newly selected output.

Encoded outputs use a silent keep-alive stream to help compatible receivers remain locked to the digital signal when playback pauses.

The last successfully selected output is saved and restored automatically after login.

## GNOME integration

The selected RPi HDMI Audio output becomes the normal GNOME audio output.

Master volume can be controlled from:

- RPi HDMI Audio
- GNOME Quick Settings
- GNOME Sound Settings
- Keyboard volume controls

GNOME Quick Settings shows the currently selected RPi HDMI Audio output instead of presenting all encoder modes at the same time.

The six individual speaker levels remain controlled by RPi HDMI Audio.

## Browser and media playback

RPi HDMI Audio works with normal desktop applications and browser audio.

It has been tested with:

- YouTube
- Chrome / Chromium
- Netflix
- Normal stereo audio
- Multichannel 5.1 audio

Applications do not need special Dolby Digital or DTS support themselves. The application provides PCM audio to PipeWire and RPi HDMI Audio performs the real-time encoding when Dolby Digital or DTS is selected.

## Netflix 5.1

**Netflix 5.1 has been tested and works.**

On Chrome/Chromium I use the **New Netflix 1080p** extension:

https://chromewebstore.google.com/detail/new-netflix-1080p/mdlbikciddolbenfkgggdegphnhmnfcg

With a Netflix title that provides a **5.1 audio track**, RPi HDMI Audio can use the multichannel audio and encode it in real time to **Dolby Digital 5.1**.

This allows browser-based Netflix 5.1 audio to be used with a compatible Dolby Digital receiver.

<img height="400" alt="Netflix 5.1" src="https://github.com/user-attachments/assets/b6562f12-3361-458d-a1be-a981d6562b79" /> <img height="400" alt="Dolby Digital 5.1 output" src="https://github.com/user-attachments/assets/3f0c1bcc-b113-4791-a760-df880dc4f350" />

## Platform

Currently developed and tested on:

- Raspberry Pi 5 / Raspberry Pi 500+
- ARM64
- Debian 13
- GNOME
- PipeWire / WirePlumber
- 48 kHz audio

Dolby Digital and DTS output require compatible decoding hardware.

PCM 5.1 support depends on the capabilities reported by the connected HDMI device.

## Upgrade / install

You do **not** need to uninstall the previous version first.

The installer overwrites the application and launcher, removes known obsolete experimental files, and preserves the current or previously saved selection.

```bash
chmod +x install-rpi-hdmi-audio.sh
sudo ./install-rpi-hdmi-audio.sh
```
## Remove

RPi HDMI Audio can be removed with the included removal script:

~~~bash
chmod +x remove-rpi-hdmi-audio.sh
sudo ./remove-rpi-hdmi-audio.sh
~~~

The removal script removes RPi HDMI Audio, its launcher and background components, and restores the normal system HDMI audio configuration.
