# Doom on the LED panel

Chocolate Doom runs in the local browser; its framebuffer is streamed through
our existing Python / USB connection to the ESP32-S3 at 30 FPS.
The board remains a 48×48 display controller. The browser shows the full game;
the browser preview and LEDs show the complete image scaled to fill 48×48,
without cropping or letterbox bars. The browser Fullscreen button fits this
square preview to the available screen. The engine framebuffer is separate
from the display canvas, with SDL aspect correction disabled. The browser adapter
keeps SDL's pixel-ratio query at 1 so Retina displays and browser zoom cannot
enlarge the renderer's coordinates and crop the native framebuffer.
Doom temporarily selects the compressed 30 FPS output path; stopping it
restores the previous effect's rate and firmware streaming mode.
No internet connection or npm installation is needed to play.

Open the control app's **Doom** tab, click **Play Doom**, then use the keyboard.
W/S or Up/Down move, A/D strafe, Left/Right turn, Ctrl fires, Space opens doors,
Shift runs, 1–7 choose weapons, Enter selects, and Esc opens the game menu.
Click the game to focus its keyboard. Stop releases the board and pauses the
game; Play Doom resumes it. Leaving the tab ends the browser session.
Browser saves currently last only for
that session. Brightness comes from Audio palette; the firmware limits apply.
Closing the browser or losing frame uploads releases the display after 2 seconds.
The browser can also play in board-free video preview mode.

## Copied upstream code and assets

Copied from https://github.com/gabrielbotandev/doom-wasm
at commit `64de0924591dec59a7d49a7d10467e125b50ea99`:

- `vendor/chocolate-doom/`: complete corresponding C engine source (GPL-2.0-or-later).
- `web/engine/`: unmodified prebuilt JavaScript, WebAssembly and data bundle.
- `assets/freedoom2.wad`: Freedoom 0.13.0 free game data; also in the data bundle.
- `scripts/build_wasm.sh`: upstream Emscripten build script, with local paths adjusted.
- `licenses/`: engine, Freedoom credits/license, SDL2 and Emscripten notices.

Our browser integration in `web/index.html` and `web/player.js` is
GPL-2.0-or-later. See LICENSE and licenses/THIRD_PARTY_NOTICES.md.
`SHA256SUMS` records the copied binary/assets' hashes. Original commercial Doom
levels and assets are not bundled. Source provenance is also in UPSTREAM.md.

## Rebuild the copied engine

Install Emscripten 4.0.14, CMake and Ninja; activate emsdk in your shell, then:

```bash
bash doom_game/scripts/build_wasm.sh
```

The script compiles the copied source and preloads the copied Freedoom WAD,
writing the three output files to `doom_game/web/engine/`. See the original
upstream build instructions for installing the tools.

## Browser regression check

With Node, Chrome and `playwright-core` installed, run:

```bash
DOOM_PLAYWRIGHT_MODULE=/absolute/path/to/node_modules/playwright-core \
DOOM_CHROME=/absolute/path/to/chrome \
.venv/bin/python -m unittest discover -s tests -p test_doom_browser.py
```

The test starts an isolated server without connecting to the board. At display
densities 1, 1.25, 1.5, 2 and 3, it compares every native title pixel against the
original Freedoom asset, checks fullscreen and resizing, verifies the 48×48
preview matches the LED upload, and measures browser uploads at 30 FPS.
