#!/usr/bin/env python3
"""Exercise every LCD sub-pixel with non-static patterns to reduce image retention."""

from __future__ import annotations

import argparse
import colorsys
import time

import numpy as np
from PIL import Image

from lcd_hat_dashboard import HEIGHT, WIDTH, ST7789


def recovery_frame(frame_number: int, fps: float) -> Image.Image:
    """Return a constantly moving RGB/grey pattern without fixed UI edges."""
    seconds = frame_number / fps
    x = np.arange(WIDTH, dtype=np.float32)[None, :]
    y = np.arange(HEIGHT, dtype=np.float32)[:, None]
    phase = seconds * 42.0

    # Alternate smoothly between a moving colour field, reversing grey ramps,
    # and fine moving noise. No pattern remains stationary for more than a
    # fraction of a second.
    section = int(seconds // 8) % 3
    if section == 0:
        hue = ((x * 1.7 + y * 0.9 + phase * 3.2) % 360.0) / 360.0
        rgb = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
        flat_hue = hue.reshape(-1)
        colours = np.array(
            [colorsys.hsv_to_rgb(float(value), 0.90, 0.92) for value in flat_hue],
            dtype=np.float32,
        ).reshape(HEIGHT, WIDTH, 3)
        rgb[:] = np.rint(colours * 255.0).astype(np.uint8)
    elif section == 1:
        grey = ((x * 2.0 + y * 1.0 + phase * 5.0) % 256.0).astype(np.uint8)
        if int(seconds * 2) & 1:
            grey = 255 - grey
        rgb = np.repeat(grey[:, :, None], 3, axis=2)
    else:
        # Deterministic moving high-frequency pattern, gentler than full-screen
        # white/black flashing while still changing every pixel each frame.
        checker = ((x.astype(np.int16) + y.astype(np.int16) + frame_number) & 1)
        wave = 72.0 * np.sin((x + phase) * 0.19) + 72.0 * np.sin((y - phase) * 0.17)
        grey = np.clip(128.0 + wave + (checker * 34 - 17), 16, 240).astype(np.uint8)
        rgb = np.stack((grey, np.roll(grey, frame_number % 17, axis=1),
                        np.roll(grey, frame_number % 13, axis=0)), axis=2)

    return Image.fromarray(rgb, "RGB").rotate(90)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=1800.0,
                        help="recovery duration in seconds (default: 1800)")
    parser.add_argument("--fps", type=float, default=12.0,
                        help="frames per second (default: 12)")
    parser.add_argument("--preview", help="write one sample frame instead of using GPIO/SPI")
    args = parser.parse_args()
    fps = max(1.0, min(20.0, args.fps))

    if args.preview:
        recovery_frame(37, fps).save(args.preview)
        return

    display = ST7789()
    started = time.monotonic()
    frame_number = 0
    try:
        while time.monotonic() - started < max(1.0, args.duration):
            target = started + frame_number / fps
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            display.show(recovery_frame(frame_number, fps))
            frame_number += 1
    finally:
        # Finish on a clean black frame before the normal dashboard restarts.
        display.show(Image.new("RGB", (WIDTH, HEIGHT), "black"))
        display.close()


if __name__ == "__main__":
    main()
