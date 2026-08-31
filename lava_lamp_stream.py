#!/usr/bin/env python3
"""Simulate warm lava-lamp fluid and stream it to a 16x16 ESP32 panel."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Iterator

import numpy as np

from export_arduino import encode_rgb565
from stream_arduino import (
    DEFAULT_PORT_WAIT,
    DEFAULT_STREAM_FPS,
    PACKET_CLEAR,
    SERIAL_BAUD,
    STATUS_ACK,
    FrameSource,
    SerialConnection,
    exchange_packet,
    handshake,
    open_serial,
    resolve_port,
    stream_frames,
)


PANEL_SIZE = 16
DEFAULT_SIMULATION_SIZE = 48
DEFAULT_SEED = 2026


def _smoothstep(low: float, high: float, values: np.ndarray) -> np.ndarray:
    amount = np.clip((values - low) / (high - low), 0.0, 1.0)
    return amount * amount * (3.0 - 2.0 * amount)


class LavaLampFluid:
    """Small stable-fluids solver with heat, buoyancy, and colored dye.

    Velocity is made divergence-free with a pressure projection. Density and
    temperature then move through that velocity field with semi-Lagrangian
    advection. Hot dye rises; after cooling, its slight weight pulls it down.
    """

    def __init__(
        self,
        size: int = DEFAULT_SIMULATION_SIZE,
        *,
        fps: float = DEFAULT_STREAM_FPS,
        seed: int = DEFAULT_SEED,
        speed: float = 1.0,
        brightness: float = 1.0,
        warmup_seconds: float = 2.5,
    ) -> None:
        if size < PANEL_SIZE or size % PANEL_SIZE:
            raise ValueError("simulation size must be a multiple of 16")
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("FPS must be a positive finite number")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed must be a positive finite number")
        if not math.isfinite(brightness) or not 0 < brightness <= 1:
            raise ValueError("brightness must be greater than 0 and at most 1")
        if not math.isfinite(warmup_seconds) or warmup_seconds < 0:
            raise ValueError("warmup must be a non-negative finite number")

        self.size = size
        self.fps = fps
        self.dt = min(speed / fps, 1.0 / 20.0)
        self.brightness = brightness
        self.rng = np.random.default_rng(seed)
        self.elapsed = 0.0

        coordinates = np.arange(size, dtype=np.float32)
        self.x, self.y = np.meshgrid(coordinates, coordinates)
        self.velocity_x = np.zeros((size, size), dtype=np.float32)
        self.velocity_y = np.zeros((size, size), dtype=np.float32)
        self.density = np.zeros((size, size), dtype=np.float32)
        self.temperature = np.zeros((size, size), dtype=np.float32)

        # Each emitter gets a different rhythm so the blobs join and separate
        # instead of rising in three synchronized, repeating columns.
        self.emitter_x = np.array((0.23, 0.50, 0.77), dtype=np.float32) * (size - 1)
        self.emitter_phase = self.rng.uniform(0.0, 2.0 * np.pi, 3)
        self.emitter_rate = self.rng.uniform(0.48, 0.78, 3)
        self.emitter_drift = self.rng.uniform(0.12, 0.28, 3)

        warmup_frames = round(warmup_seconds * fps)
        for _ in range(warmup_frames):
            self.step()

    @staticmethod
    def _neighbors(field: np.ndarray) -> tuple[np.ndarray, ...]:
        padded = np.pad(field, 1, mode="edge")
        return (
            padded[1:-1, :-2],
            padded[1:-1, 2:],
            padded[:-2, 1:-1],
            padded[2:, 1:-1],
        )

    def _sample(
        self, field: np.ndarray, sample_x: np.ndarray, sample_y: np.ndarray
    ) -> np.ndarray:
        sample_x = np.clip(sample_x, 0.0, self.size - 1.001)
        sample_y = np.clip(sample_y, 0.0, self.size - 1.001)
        x0 = sample_x.astype(np.int32)
        y0 = sample_y.astype(np.int32)
        x1 = x0 + 1
        y1 = y0 + 1
        fraction_x = sample_x - x0
        fraction_y = sample_y - y0
        return (
            field[y0, x0] * (1.0 - fraction_x) * (1.0 - fraction_y)
            + field[y0, x1] * fraction_x * (1.0 - fraction_y)
            + field[y1, x0] * (1.0 - fraction_x) * fraction_y
            + field[y1, x1] * fraction_x * fraction_y
        ).astype(np.float32)

    def _advect(self, field: np.ndarray) -> np.ndarray:
        source_x = self.x - self.dt * self.velocity_x
        source_y = self.y - self.dt * self.velocity_y
        return self._sample(field, source_x, source_y)

    def _project_velocity(self, iterations: int = 14) -> None:
        left, right, up, down = self._neighbors(self.velocity_x)
        divergence = 0.5 * (right - left)
        left, right, up, down = self._neighbors(self.velocity_y)
        divergence += 0.5 * (down - up)

        pressure = np.zeros_like(divergence)
        for _ in range(iterations):
            left, right, up, down = self._neighbors(pressure)
            pressure = (left + right + up + down - divergence) * 0.25

        left, right, up, down = self._neighbors(pressure)
        self.velocity_x -= 0.5 * (right - left)
        self.velocity_y -= 0.5 * (down - up)
        self._apply_boundaries()

    def _apply_boundaries(self) -> None:
        self.velocity_x[:, (0, -1)] = 0.0
        self.velocity_y[(0, -1), :] = 0.0
        self.velocity_x[(0, -1), :] *= 0.35
        self.velocity_y[:, (0, -1)] *= 0.35

    def _add_emitters(self) -> None:
        radius = self.size * 0.075
        source_y = self.size * 0.91
        for index in range(3):
            phase = self.emitter_phase[index] + self.elapsed * self.emitter_rate[index]
            center_x = self.emitter_x[index] + self.size * self.emitter_drift[
                index
            ] * math.sin(phase * 0.43 + index)
            distance_squared = (self.x - center_x) ** 2 + (self.y - source_y) ** 2
            source = np.exp(-distance_squared / (2.0 * radius * radius))
            pulse = max(0.0, math.sin(phase)) ** 3
            strength = 0.035 + 0.82 * pulse
            self.density += self.dt * strength * source
            self.temperature += self.dt * (0.24 + 2.3 * pulse) * source
            self.velocity_y -= self.dt * (5.0 + 16.0 * pulse) * source
            self.velocity_x += (
                self.dt * 2.0 * math.sin(phase * 1.7) * source
            )

    def step(self) -> None:
        """Advance the fluid by one output-frame interval."""
        self._add_emitters()

        # Hot material rises. Dye keeps a small downward weight, so cooled
        # patches curl back toward the heater like a physical lava lamp.
        self.velocity_y += self.dt * (
            2.2 * self.density - 10.5 * self.temperature
        )

        # Curl confinement restores the rolling motion normally lost through
        # numerical diffusion on a tiny grid.
        left, right, up, down = self._neighbors(self.velocity_y)
        curl = 0.5 * (right - left)
        left, right, up, down = self._neighbors(self.velocity_x)
        curl -= 0.5 * (down - up)
        magnitude = np.abs(curl)
        left, right, up, down = self._neighbors(magnitude)
        gradient_x = 0.5 * (right - left)
        gradient_y = 0.5 * (down - up)
        length = np.sqrt(gradient_x * gradient_x + gradient_y * gradient_y) + 1e-5
        self.velocity_x += self.dt * 1.15 * gradient_y / length * curl
        self.velocity_y -= self.dt * 1.15 * gradient_x / length * curl

        self.velocity_x = self._advect(self.velocity_x)
        self.velocity_y = self._advect(self.velocity_y)

        # A light explicit viscosity keeps one-pixel turbulence from flickering.
        for field in (self.velocity_x, self.velocity_y):
            left, right, up, down = self._neighbors(field)
            field += self.dt * 0.16 * (left + right + up + down - 4.0 * field)
            np.clip(field, -self.size * 0.45, self.size * 0.45, out=field)
        self._project_velocity()

        self.density = self._advect(self.density)
        self.temperature = self._advect(self.temperature)
        self.density *= math.exp(-0.11 * self.dt)
        self.temperature *= math.exp(-0.34 * self.dt)

        # Material reaching the cap fades away, keeping the simulation in a
        # steady cycle instead of eventually filling the entire display.
        top_fade = np.clip(self.y / (self.size * 0.18), 0.72, 1.0)
        self.density *= top_fade
        self.temperature *= top_fade
        np.clip(self.density, 0.0, 1.2, out=self.density)
        np.clip(self.temperature, 0.0, 1.2, out=self.temperature)
        self.elapsed += self.dt

    def _panel_field(self, field: np.ndarray) -> np.ndarray:
        scale = self.size // PANEL_SIZE
        return field.reshape(PANEL_SIZE, scale, PANEL_SIZE, scale).mean(axis=(1, 3))

    def render(self) -> np.ndarray:
        """Advance and return one 16x16 RGB frame in the warm lava palette."""
        self.step()
        density = self._panel_field(self.density)
        heat = self._panel_field(self.temperature)

        coverage = _smoothstep(0.025, 0.52, density)
        hot_core = _smoothstep(0.10, 0.88, density * 0.72 + heat * 0.58)
        red = 255.0 * coverage
        green = coverage * (8.0 + 235.0 * hot_core**1.25)
        blue = np.zeros_like(red)
        frame = np.stack((red, green, blue), axis=2) * self.brightness
        return np.rint(frame).clip(0, 255).astype(np.uint8)


def iter_lava_frames(simulation: LavaLampFluid) -> Iterator[bytes]:
    while True:
        yield encode_rgb565(simulation.render())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate fluid lava and stream it to a 16x16 ESP32 LED panel"
    )
    parser.add_argument(
        "--port", default="auto", help="serial port (default: auto-detect)"
    )
    parser.add_argument(
        "--port-wait",
        type=float,
        default=DEFAULT_PORT_WAIT,
        help="seconds to wait for an auto-detected board (default: 30)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_STREAM_FPS,
        help="LED refresh rate (default: 30)",
    )
    parser.add_argument(
        "--simulation-size",
        type=int,
        default=DEFAULT_SIMULATION_SIZE,
        help="internal fluid-grid size; multiple of 16 (default: 48)",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="fluid motion speed (default: 1)"
    )
    parser.add_argument(
        "--brightness",
        type=float,
        default=1.0,
        help="software brightness from 0 to 1 (default: 1)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--warmup",
        type=float,
        default=2.5,
        help="simulated seconds prepared before display starts (default: 2.5)",
    )
    parser.add_argument(
        "--timeout", type=float, default=1.0, help="response timeout (default: 1)"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="packet retries (default: 3)"
    )
    parser.add_argument(
        "--clear-on-exit", action="store_true", help="turn LEDs off on exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("error: --fps must be a positive finite number")
    if args.simulation_size < PANEL_SIZE or args.simulation_size % PANEL_SIZE:
        raise SystemExit("error: --simulation-size must be a multiple of 16")
    if not math.isfinite(args.speed) or args.speed <= 0:
        raise SystemExit("error: --speed must be a positive finite number")
    if not math.isfinite(args.brightness) or not 0 < args.brightness <= 1:
        raise SystemExit("error: --brightness must be greater than 0 and at most 1")
    if not math.isfinite(args.warmup) or args.warmup < 0:
        raise SystemExit("error: --warmup must be a non-negative finite number")
    if args.port_wait < 0:
        raise SystemExit("error: --port-wait cannot be negative")
    if args.timeout <= 0:
        raise SystemExit("error: --timeout must be positive")
    if args.retries < 0:
        raise SystemExit("error: --retries cannot be negative")

    connection: SerialConnection | None = None
    try:
        print("Preparing the lava fluid...", file=sys.stderr)
        simulation = LavaLampFluid(
            args.simulation_size,
            fps=args.fps,
            seed=args.seed,
            speed=args.speed,
            brightness=args.brightness,
            warmup_seconds=args.warmup,
        )
        source = FrameSource(
            fps=args.fps, iter_frames=lambda: iter_lava_frames(simulation)
        )
        port = resolve_port(args.port, wait_timeout=args.port_wait)
        print(f"Opening {port} at {SERIAL_BAUD} baud...", file=sys.stderr)
        connection = open_serial(port, args.timeout)
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3))
        print(
            f"Streaming lava-lamp fluid at {source.fps:g} FPS. Ctrl-C stops.",
            file=sys.stderr,
        )
        stream_frames(
            connection,
            source,
            loop=False,
            timeout=args.timeout,
            retries=args.retries,
            drop_late=True,
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            if args.clear_on_exit:
                try:
                    exchange_packet(
                        connection,
                        PACKET_CLEAR,
                        0xFFFFFFFF,
                        b"",
                        STATUS_ACK,
                        args.timeout,
                        0,
                    )
                except (OSError, RuntimeError):
                    pass
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
