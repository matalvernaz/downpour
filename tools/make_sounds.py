"""Generate Downpour's short notification sounds (stdlib only).

    py tools/make_sounds.py

Writes small 16-bit mono WAV files into downpour/sounds/. They are committed,
so this only needs re-running to change how they sound.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "downpour" / "sounds"


def tone(freq: float, seconds: float, volume: float = 0.35) -> list[float]:
    n = int(RATE * seconds)
    out = []
    for i in range(n):
        t = i / RATE
        attack = min(1.0, t / 0.008)
        decay = math.exp(-4.5 * t / seconds)
        sample = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
        out.append(volume * attack * decay * sample / 1.25)
    return out


def silence(seconds: float) -> list[float]:
    return [0.0] * int(RATE * seconds)


def write(name: str, samples: list[float]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))


def main() -> None:
    # Rising major third then fifth: done, and it went well.
    write("complete.wav", tone(659.25, 0.12) + tone(830.61, 0.12) + tone(987.77, 0.28))
    # Falling minor second: something needs attention.
    write("failed.wav", tone(392.0, 0.16) + silence(0.03) + tone(369.99, 0.32))
    # One soft blip: a download was added.
    write("added.wav", tone(880.0, 0.07, volume=0.25))
    # Gentle two-note chime: the whole queue is finished.
    write("queue-done.wav", tone(523.25, 0.18) + tone(783.99, 0.18) + tone(1046.5, 0.45))
    print(f"Wrote sounds to {OUT}")


if __name__ == "__main__":
    main()
