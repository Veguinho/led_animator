# projectM on the LED display

The **projectM** tab uses the real libprojectM 4.1.6 engine and the Cream of the
Crop MilkDrop presets. It captures the app's existing Mac system audio, renders
into a hidden SDL2/OpenGL window at **192×192**, averages each 4×4 block into
one **48×48** RGB pixel, flips OpenGL's vertical orientation, and sends the
result through the existing RGB565 USB stream. The preview shows the encoded
LED frame with a uniform display gain. Shared brightness and saturation apply;
the preset supplies its own colors. Existing firmware limits still apply.

## Setup

Requires CMake 3.21+, Git, a C/C++ compiler, SDL2 development libraries and
desktop OpenGL 3.3. On macOS, install Command Line Tools and use
`brew install cmake sdl2` if these are missing. The normal Python requirements
remain sufficient; there are no new Python packages.

From the repository root:

```sh
.venv/bin/python scripts/setup_projectm.py
```

The setup script clones the requested SDL frontend, the engine, its expression
evaluator submodule, the preset pack and textures into `.build/projectm/vendor/`.
Revisions are pinned in the script. It builds a small LED-specific host instead
of the upstream desktop UI. The frontend is retained as a reference; its Poco,
ImGui and desktop audio capture components are replaced by our existing app.
The native engine remains dynamically linked, with its source and upstream
license files alongside the build. Setup is repeatable and verifies the cloned
revisions. No network access or build is needed during playback.

Restart the app after setup or code changes and open
`http://127.0.0.1:8765/#projectm`. Choose a preset and click **Start projectM**.
The same tab is available during video playback; starting projectM releases the
video worker and opens the live audio renderer using the same browser port.
**Return to audio visualizer** restores wave/spectrum. Starting Doom Fire
stops projectM. Opening other tabs keeps projectM playing; Doom temporarily
takes over the output and restores projectM when stopped.

You can also start directly with the existing live firmware:

```sh
./start.sh --no-upload --style projectm
```

The output cadence follows the audio stream (`--fps`, normally 30 FPS).
Automatic preset changes default to 30 seconds with two-second soft transitions.
Previous/next and the dropdown cover the complete 9,795-preset pack. Search
shows every matching preset without a result limit. **Randomize** plays
one random preset from the full library and turns off automatic changes, keeping
it playing until you choose another preset or enable automatic changes again.
Use **Add to favorites** to save the selected preset, or **Remove from favorites**
to remove it. The **Favorites** section lists all saved presets independently of
search; click one to play it with automatic changes disabled. Favorites persist
across app restarts and library reordering in `.build/projectm-favorites.json`.
Software reloads retain the selected preset and the automatic-change settings
while projectM is active. An unavailable build disables Start; native errors
appear in the tab and return output to the normal audio visualizer. Choose a
different preset to recover from an incompatible shader. The renderer's pipe
requests have a five-second timeout and its process is closed on stop/reload.
Some very detailed presets naturally lose detail on a 48×48 panel.

## Upstream sources and notices

- [SDL frontend](https://github.com/projectM-visualizer/frontend-sdl-cpp), revision
  `4f729b07abd71b6cd304e81b0adc708beb98a622`, GPL-3.0; reference clone only,
  its source is not compiled into the LED host.
- [libprojectM](https://github.com/projectM-visualizer/projectm), v4.1.6,
  `3158ee615eaafd93a8912b5f6dd84a9c47b2e00a`, LGPL-2.1-or-later; see its
  `LICENSE.txt` and bundled component notices.
- [Cream of the Crop](https://github.com/projectM-visualizer/presets-cream-of-the-crop),
  `0180df21f5e0bd39b9060cc5de420ed2f1f9e509`. Presets retain their authors'
  names; see the pack's `LICENSE.md` for its licensing statement.
- [MilkDrop textures](https://github.com/projectM-visualizer/presets-milkdrop-texture-pack),
  `6368812f27bc747b517218fbf89d21d59afce4d9`; see the upstream repository.
- [SDL2](https://github.com/libsdl-org/SDL), zlib license, installed separately.

The new native host is original code using projectM's public C API, following
the SDL frontend's context/audio/render sequence. Source dependencies and their
notices remain in the ignored build directory rather than being copied into
the app's tracked files.
