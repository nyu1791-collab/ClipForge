#!/usr/bin/env python3
"""Generate original CC0 demo sounds/frame; requires only the Python stdlib."""

import math
import struct
import wave
import zlib
from pathlib import Path


def generate(root):
    root = Path(root)
    for d in ("fonts", "se", "bgm", "overlays"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for name, seconds in [("bgm/demo-soft.wav", 12), ("se/demo-hook.wav", 0.35)]:
        with wave.open(str(root / name), "wb") as out:
            rate = 24000
            out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            frames = bytearray()
            for i in range(round(rate * seconds)):
                t = i / rate
                if "bgm" in name:
                    notes = [130.81, 164.81, 196, 164.81]
                    tone = (
                        sum(
                            math.sin(2 * math.pi * f * t)
                            for f in (notes[int(t // 3) % 4], 261.63, 392)
                        )
                        / 3
                    )
                    envelope = min(1, t * 3, (seconds - t) * 3)
                else:
                    tone = math.sin(2 * math.pi * (180 * t - 100 * t * t))
                    envelope = math.exp(-15 * t)
                frames += struct.pack("<h", round(32767 * 0.3 * tone * envelope))
            out.writeframes(frames)
    width, height = 540, 960
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            edge = x < 5 or x >= width - 5 or y < 5 or y >= height - 5
            rows.extend((255, 205, 35, 200) if edge else (0, 0, 0, 0))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    (root / "overlays/demo-frame.png").write_bytes(png)


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "assets")
