# Rolling-buffer 48×48 MP4 player

Before the first playback, this mode uses FFmpeg to center-crop the source,
blend it down to 48×48, expand black/white levels, increase color saturation,
then apply a smooth S-curve that darkens midtones and lifts highlights. It
caches the result as a tiny LED-ready MP4; later launches reuse that file.

Streaming does **not** load the whole prepared video. A bounded queue defaults
to three seconds and waits for one second of prebuffer before playback. FFmpeg
blocks when the queue is full, keeping RAM approximately constant even for
multi-hour videos. Playback defaults to a stable 6 FPS on the current hardware.

```bash
python main.py mp4 video_clips/clip.mp4
python main.py mp4 video_clips/clip.mp4 --no-upload
python main.py mp4 video_clips/clip.mp4 --no-upload --no-browser
```

Each 48×48 RGB565 frame is 4,608 bytes. At the stable 6 FPS default, the
three-second queue holds only 18 frames, about 81 KiB of pixel payload. Keep the host process running while
the video plays; use `--loop` to start it again after the final frame.

The local page at `http://127.0.0.1:8765/video` intentionally exposes only
**Play** and **Pause**. Color enhancement happens once before streaming, not
live in the browser or frame loop, so it does not add ongoing CPU load.

`player.py` retains the short-clip PSRAM preloader as a compatibility tool, but
the `main.py mp4` mode uses `stream.py` and the bounded live-stream transport.
