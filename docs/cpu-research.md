# Video CPU research

The live video path uses VideoToolbox automatically on macOS, with software
fallback if hardware decoding fails before the first frame. The area scaler,
48×48 output, source frame rate, live audio overlay, brightness protection and
serial acknowledgements remain active. Hardware decoding uses three threads,
software fallback uses two, and the filter graph uses one. Stream compression
uses level 1. See the [FFmpeg hardware acceleration and filter thread
documentation](https://www.ffmpeg.org/ffmpeg.html).

The web preview requests RGB bytes as base64 instead of thousands of JSON
numbers, reuses connections, caches the encoded frame and redraws only when a
new acknowledged frame arrives. Its video refresh follows the source FPS.
The original video preview pauses when its disclosure is closed; opening it
resumes at the LED timeline. Reload an already open controls tab to load the
new JavaScript.

The overlay interpolates transported audio envelopes once per distinct radius,
then expands the results back to the grid. The audio capture uses a circular
buffer, energy tracking keeps sliding maxima, and RGB565 brightness checks use
a precomputed table. These transformations preserve the previous arithmetic.

Run the measured parameter search from the checkout:

```sh
.venv/bin/python scripts/research_video_cpu.py 'wex.muzik PARTAE.square.mp4' \
  --start 2100 --seconds 6 --warmup 2 --poll-fps 29.97002997002997 \
  --compact --patience 50 --output .build/cpu-research/trials.jsonl
```

Each worker runs the real FFmpeg decoder, changing deterministic audio,
circular overlay, gamma mapping, flash limiter, RGB565 encoding, compression,
preview publication and HTTP server at the original playback rate. An HTTP
client in a separate process supplies preview load. The objective is
`100 × (Python CPU seconds + FFmpeg CPU seconds) / measured wall seconds`;
100% represents one logical core. Initialization and warmup are excluded.
Audio capture and browser rendering are measured separately in the live check.
The benchmark does not open the board's serial port or include USB ACK wait time.

Trials below 97% of source FPS or with preview failures are rejected. Decoder
threads, filter threads, hardware/software decoding and lossless compression
levels are explored. An apparent CPU improvement is confirmed with three
measurements and their median. A gain must exceed 1% relative to the incumbent.
The counter resets after an accepted gain; the search stops after exactly 50
consecutive attempts without one. Configurations can be revisited to verify
the plateau. The script records every attempt and writes a summary beside the
JSONL log; it does not edit production code or persist tuning preferences.

macOS CPU speed limits are recorded with each trial because temperature and
other running applications can change measured percentages. This establishes
a measured minimum among the attempted configurations, not a proof of a global
minimum. Repeat the live board check after applying any selected parameters.

For physical image validation, capture the C920 camera with FFmpeg's
AVFoundation input while the stream and both layers remain active. Allow the
camera to settle before evaluating its first frames. A warmed four-second
recording and an accompanying sequence of acknowledged preview frames are in
`.build/cpu-research/webcam-validation/`. Camera exposure and perspective
prevent an exact pixel comparison; check the visible image structure, colors
and movement together with the serial ACK/FPS log.

For a single trial or an optional profile:

```sh
.venv/bin/python scripts/research_video_cpu.py 'wex.muzik PARTAE.square.mp4' \
  --worker --start 2100 --seconds 10 --warmup 2 \
  --config '{"poll_fps":30,"compact":true,"compression":3}'
```

`--profile PATH` saves a cProfile file. Profiled CPU numbers should not be used
for before/after comparisons because profiling adds overhead.
