# Epilogue GB Player

A local laptop app that detects and reads an original GB/GBC cartridge through
the Epilogue GB Operator, then plays music for cartridges with a verified
playback profile. The prototype runs a small browser UI on localhost and uses
Throwback for read-only cartridge access plus gbsplay for audio.

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

## Current support

The first profile supports the exact North American Operation C image present
in the user's collection and currently inserted in the Operator. The app
matches the full ROM SHA-256 before enabling playback, checks the GBS module's
SHA-256 and verifies its 14-subtune header. Its track labels are shown in the
list. The GBS archive is fetched on first use and cached under
`~/.local/share/epilogue-player/gbs/`; it is not included in this source tree.

The archive entry is from the [GBS Penultimate Archive](https://snesmusic.org/hoot/gbs/).
Track labels are from the [Operation C soundtrack index](https://downloads.khinsider.com/game-soundtracks/album/operation-c-gb).
The full ROM dump remains on the host and is never uploaded by this app.

Other cartridges are identified and dumped, but the app reports them as
unsupported until a matching profile exists. A profile maps exact ROM hashes
to a GBS file, its checksum, track labels, and source in
`profiles/catalog.json`. This profile system can grow without changing the UI
or the Operator code. It is not a universal ROM music extractor: many games
need driver-specific ripping work before their music can be played this way.

The GB Operator cartridge transfer was tested locally. Operation C playback
was verified through the PulseAudio-compatible output and the app's play,
pause, resume, stop, and next-track controls. This prototype is read-only with
respect to cartridge contents; it never writes ROM or save data.
