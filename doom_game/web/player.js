// SPDX-License-Identifier: GPL-2.0-or-later
// Uses the vendored Chocolate Doom Emscripten interface from gabrielbotandev/doom-wasm.
const canvas = document.getElementById("game");
const engineCanvas = document.getElementById("engine");
const play = document.getElementById("play");
const stop = document.getElementById("stop");
const status = document.getElementById("status");
const fullscreen = document.getElementById("fullscreen");
let engine, token, timer, running = false, starting = false;
let loopReady = false;
let generation = 0;
let size = 48;
const FRAME_PERIOD = 1000 / 30;
const panel = document.createElement("canvas");
const ctx = panel.getContext("2d", { willReadFrequently: true });

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: AbortSignal.timeout(5000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Doom connection failed");
  return data;
}

// Keep one upload in flight. The server also holds only the latest frame.
async function uploadFrame(currentGeneration) {
  if (!running || currentGeneration !== generation) return;
  const startedAt = performance.now();
  try {
    ctx.fillStyle = "black";
    ctx.fillRect(0, 0, size, size);
    ctx.imageSmoothingEnabled = true;
    // Scale the entire engine framebuffer to the complete square display.
    ctx.drawImage(engineCanvas, 0, 0, engineCanvas.width, engineCanvas.height, 0, 0, size, size);
    const preview = canvas.getContext("2d");
    preview.imageSmoothingEnabled = false;
    preview.drawImage(panel, 0, 0);
    const rgba = ctx.getImageData(0, 0, size, size).data;
    let rgb = "";
    for (let i = 0; i < rgba.length; i += 4) rgb += String.fromCharCode(rgba[i], rgba[i + 1], rgba[i + 2]);
    await post("/api/doom/frame", { token, rgb: btoa(rgb) });
    if (running && currentGeneration === generation) timer = setTimeout(
      () => uploadFrame(currentGeneration), Math.max(0, FRAME_PERIOD - (performance.now() - startedAt)));
  } catch (error) {
    if (currentGeneration !== generation) return;
    stopDoom();
    status.textContent = error.message + " · click Play Doom to reconnect";
  }
}

function stopDoom() {
  running = false;
  starting = false;
  generation++;
  clearTimeout(timer);
  if (loopReady) engine?.pauseMainLoop?.();
  engine?.SDL2?.audioContext?.suspend?.();
  if (token) navigator.sendBeacon("/api/doom/stop", new Blob([JSON.stringify({ token })], { type: "application/json" }));
  token = null;
  play.disabled = false;
  stop.disabled = true;
  fullscreen.disabled = true;
  status.textContent = "Stopped · previous LED display restored";
}
window.stopDoom = stopDoom;
window.addEventListener("pagehide", stopDoom);

play.addEventListener("click", async () => {
  if (running || starting) return;
  starting = true;
  play.disabled = true;
  status.textContent = "Loading Doom…";
  const currentGeneration = ++generation;
  try {
    // Load the large local bundle before leasing the display.
    if (!engine) {
      const [{ default: createDoomModule }, compiled] = await Promise.all([
        import("/doom/engine/chocolate-doom.js"),
        fetch("/doom/engine/chocolate-doom.wasm").then(async response => {
          if (!response.ok) throw new Error("Could not load the Doom engine");
          return WebAssembly.compile(await response.arrayBuffer());
        }),
      ]);
      engine = await createDoomModule({
        canvas: engineCanvas, keyboardListeningElement: canvas, noInitialRun: true,
        instantiateWasm: (imports, receiveInstance) => {
          // SDL's software renderer uses a fixed 320×200 framebuffer. Its
          // high-DPI window flag otherwise doubles rendering coordinates on
          // Retina displays and crops the image inside that framebuffer.
          // Override only SDL's host pixel-ratio query; the visible canvas and
          // the browser keep their real display density. Upstream stays intact.
          let overrides = 0;
          for (const namespace of Object.values(imports)) {
            for (const [name, value] of Object.entries(namespace)) {
              if (typeof value === "function" && value.name === "_emscripten_get_device_pixel_ratio") {
                namespace[name] = () => 1;
                overrides++;
              }
            }
          }
          if (overrides !== 1) throw new Error("Doom engine pixel-ratio interface changed");
          const instance = new WebAssembly.Instance(compiled, imports);
          receiveInstance(instance, compiled);
          return instance.exports;
        },
        postMainLoop: () => {
          loopReady = true;
          if (!running) engine?.pauseMainLoop();
        },
        locateFile: path => "/doom/engine/" + path,
        print: () => {}, printErr: line => console.debug("Doom:", line),
        onAbort: reason => { stopDoom(); status.textContent = "Doom stopped: " + reason; },
        preRun: [module => {
          module.FS.mkdir("/config");
          module.FS.mkdir("/savegames");
          module.FS.writeFile("/config/default.cfg", [
            "fullscreen 0", "window_width 320", "window_height 200", "grabmouse 0",
            "use_mouse 0", "key_up 119", "key_down 115", "key_strafeleft 97", "key_straferight 100",
          ].join("\n") + "\n");
          module.FS.writeFile("/config/chocolate-doom.cfg", "smooth_pixel_scaling 0\nforce_software_renderer 1\naspect_ratio_correct 0\ninteger_scaling 0\n");
        }],
      });
      // callMain initializes the game once; Stop/Play pauses/resumes it.
      try {
        engine.callMain(["-window", "-iwad", "/iwads/freedoom2.wad", "-savedir", "/savegames",
          "-config", "/config/default.cfg", "-extraconfig", "/config/chocolate-doom.cfg"]);
      } catch (error) {
        if (!/unwind|SimulateInfiniteLoop/.test(String(error))) throw error;
      }
    }
    if (currentGeneration !== generation) return;
    const session = await post("/api/doom/start", {});
    if (currentGeneration !== generation) {
      await post("/api/doom/stop", { token: session.token });
      return;
    }
    token = session.token;
    size = session.size;
    panel.width = panel.height = size;
    canvas.width = canvas.height = size;
    running = true;
    starting = false;
    if (loopReady) engine.resumeMainLoop();
    engine.SDL2?.audioContext?.resume?.();
    stop.disabled = false;
    fullscreen.disabled = false;
    status.textContent = session.connected ? "Playing · streaming to LED board" : "Playing · browser preview";
    document.getElementById("output").textContent = session.connected
      ? `${size}×${size} · 30 FPS · full frame · brightness follows Audio palette`
      : "No board connected · keyboard play is available here";
    canvas.focus();
    uploadFrame(currentGeneration);
  } catch (error) {
    if (currentGeneration !== generation) return;
    stopDoom();
    status.textContent = "Could not start Doom: " + error.message;
  }
});
stop.addEventListener("click", stopDoom);
canvas.addEventListener("click", () => canvas.focus());
fullscreen.addEventListener("click", async () => {
  try { await document.getElementById("screen").requestFullscreen(); canvas.focus(); }
  catch (error) { status.textContent = error.message; }
});

// Preserve arrow movement alongside WASD config, including release on blur.
const aliases = new Map([["ArrowUp", ["w", "KeyW", 87]], ["ArrowDown", ["s", "KeyS", 83]]]);
const held = new Map();
// SDL's generated browser runtime listens on the document. Restrict game keys
// to the focused canvas so Enter/Space on the buttons remain normal UI actions.
for (const type of ["keydown", "keyup"]) {
  document.addEventListener(type, event => {
    if (event.target !== canvas) event.stopImmediatePropagation();
  }, true);
}
function keyEvent(type, key, code, keyCode) {
  canvas.dispatchEvent(new KeyboardEvent(type, { key, code, keyCode, which: keyCode, bubbles: true }));
}
canvas.addEventListener("keydown", event => {
  if (!event.isTrusted) return;
  held.set(event.code, [event.key, event.code, event.keyCode]);
  if (aliases.has(event.code)) { event.preventDefault(); keyEvent("keydown", ...aliases.get(event.code)); }
});
canvas.addEventListener("keyup", event => {
  if (!event.isTrusted) return;
  held.delete(event.code);
  if (aliases.has(event.code)) { event.preventDefault(); keyEvent("keyup", ...aliases.get(event.code)); }
});
canvas.addEventListener("blur", () => {
  for (const [code, values] of held) {
    keyEvent("keyup", ...values);
    if (aliases.has(code)) keyEvent("keyup", ...aliases.get(code));
  }
  held.clear();
});
