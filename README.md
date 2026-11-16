# Epilogue GB Player

A local laptop app that detects and reads an original GB/GBC/GBA cartridge
through the Epilogue GB Operator, then plays music when its exact ROM image has
a matching GBS, GSF, or local rendered WAV profile. The browser UI runs on
localhost and uses Throwback for cartridge access plus gbsplay, playgsf, and
pw-play for audio.

## Run it

On this development machine, start the app with:

```sh
./run-player.sh
```

The app opens at `http://127.0.0.1:8766`. Insert a cartridge; the app polls the
Operator, reads the ROM once, validates its header and SHA-256, and loads a
matching profile. Press **PLAY**, select a track, or use previous/next. Stop the
server with Ctrl+C in the terminal where `run-player.sh` is running.

For manual startup:

```sh
python3 host-player/server.py
```

Python uses only its standard library. Throwback and gbsplay must be on `PATH`
or passed with `--throwback` and `--gbsplay`.

## Hosted browser interface

The GitHub Pages workflow publishes `host-player/web` at
`https://calebdueck.github.io/epilogue-player/`. Open it in a browser, start
`./run-player.sh` on that same computer, and click **SCAN** to connect. The
browser may ask permission to connect to localhost. The hosted page provides
the browser UI; cartridge access, ROM matching, and audio playback remain on
the computer running the local app. A different API URL can be selected with
the `?api=` query parameter.

## Music profiles and playback

The player matches full ROM SHA-256 values to a local track profile. The
catalogue has profiles for all 58 unique images in the local ROM folders: 35
GBS profiles, 13 GSF profiles, and 10 WAV profiles. Tony Hawk's Underground 2
uses a 156-second capture rendered from its exact ROM through mGBA because its
Gaxtapper GSF tracks render silence; the capture contains in-game music and
gameplay audio, not six individually separated songs.
The seven games without a compatible GBS or GSF rip have one hash-matched ROM
audio capture apiece. Those short captures make each ROM playable in the music
player, but they are not complete track catalogues for those games.
Profiles and exact image aliases are in [`profiles/library.json`](profiles/library.json),
with the GBS and GSF track data in [`profiles/catalog.json`](profiles/catalog.json)
and [`profiles/gba_music_catalog.json`](profiles/gba_music_catalog.json).

GB/GBC songs play through `gbsplay`. GBA GSF tracks play through `playgsf`, a
Linux player based on the GSF decoder; install it with
`tools/install_playgsf.sh` if it is missing. Run `tools/install_gsf_library.sh`
with an installed RAR extractor to populate the GSF Central files under
`~/.local/share/epilogue-player/gsf/`. GAX tracks were extracted from matching
local ROMs with gaxtapper where its extractor supports the game's driver.
Simpsons: Road Rage and SpongeBob SuperSponge
use MP2K slots rendered to WAV in the local `wav/` cache; `pw-play` plays those
tracks. Recreate those local files and their checksum entries with
`tools/rebuild_mp2k_wav_profiles.py` after installing
[gba-audio-tools](https://github.com/mudassarzahid/gba-audio-tools). Extracted
audio is not committed to this repository. The seven ROM audio captures were
rendered from the exact local images with `tools/rom_audio_capture.c` and mGBA;
their exact input ROM hashes, button timings, WAV paths, and checksums are in
the profile catalogues. To render another local ROM capture, install mGBA's
development headers and library, then build the helper with
`cc -O2 tools/rom_audio_capture.c -lmgba -o rom_audio_capture`. It accepts an
optional comma-separated list of `FRAME:KEYMASK` input events; the helper
comment documents the key-mask values.
The player checks exact ROM hashes and local GSF/WAV track checksums before
enabling playback. Candidate rips stay auditionable while their source revision
remains unconfirmed.

The GBS modules are from the [GBS Penultimate Archive](https://snesmusic.org/hoot/gbs/),
the [JoshW GBS archive](https://gbs.joshw.info/), and sources identified in each
profile. GBA GSF music includes [GSF Central](https://www.caitsith2.com/gsf/index.php?sortby=gamed)
and local GAX extraction. The ROM dump remains local and is never uploaded by
the app.

The transport has pixel play/pause and previous/next buttons, Space and arrow
key shortcuts, system media play/pause/previous/next actions, shuffle, and
playlist repeat. Shuffle avoids immediately repeating the current track; repeat
wraps from the end of the list to the first track. The play/pause glyph and
accessible label follow the actual player state.

## Presentation modes

The synthwave interface selects its accent palette from cartridge metadata:
the GB CGB compatibility flag produces a monochrome GB or color GBC theme, and
Throwback's GBA type/header metadata selects a 16-bit GBA theme. The GB Operator
supports GB, GBC, and GBA cartridges according to [Epilogue's official FAQ](https://www.epilogue.co/support/hardware/gb-operator-faq).
For an inserted GBA, the player reads and validates the ROM once, then matches
its exact SHA-256 to the local music catalogue.

The labeled theme previews read header metadata from local ROMs in
`~/games/gb` and `~/games/gba`; they do not switch cartridges, dump files, or
play those ROMs. For example, `http://127.0.0.1:8766/?ui-preview=gba` previews
the 16-bit look using the local Wario Land 4 header. The screenshots below are
UI previews captured from the running app.

![GB monochrome synthwave theme](docs/screenshots/gb-monochrome.png)

![GBC color synthwave theme](docs/screenshots/gbc-color.png)

![GBA 16-bit synthwave preview](docs/screenshots/gba-16bit.png)

All 58 indexed ROM images currently resolve to a playable profile. The seven
ROM-rendered profiles cover Top Rank Tennis, Traumatarium Penitent, Rhythm
Tengoku, both In The Dark revisions, Tech Deck Skateboarding, and Wicked
Plague. Their single captured segments are startup, opening, title, or menu
audio, depending on the game; extracting and labeling each game's complete
soundtrack remains follow-up work.

Previously attached GB cartridges were matched to local backups by full
SHA-256, and their playback was verified through the PulseAudio-compatible
output. Local GSF playback was checked through play, pause, resume, and
next-track transitions. This app does not write ROM or save data.

Donkey Kong Land's profile matches the user's local backup by full SHA-256 and
its GBS header and module checksum were verified locally. gbsplay rendered all
20 subtunes to WAV output, but the track-to-ROM revision mapping has not been
confirmed by listening or by a physical GB Operator insertion. It remains a
candidate profile until those checks are done.
