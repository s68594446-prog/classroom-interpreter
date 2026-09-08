from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
import wave

import numpy as np
import soxr

from .durable import Chunk


@dataclass(frozen=True)
class NormalizedFrame:
    session_id: str
    capture_epoch: int
    start_monotonic_ns: int
    pcm: bytes
    sample_rate: int = 16000
    channels: int = 1
    sample_format: str = 'PCM_S16LE'


class Normalizer:
    def __init__(self, rate: int, channels: int):
        self.rate, self.channels = rate, channels
        self.stream = soxr.ResampleStream(rate, 16000, 1, dtype='float32', quality='HQ')

    def process(self, pcm: bytes, *, last: bool = False) -> bytes:
        source = np.frombuffer(pcm, dtype='<i2').reshape(-1, self.channels)
        mono = source.astype(np.float32).mean(axis=1)
        result = self.stream.resample_chunk(mono, last=last)
        return np.clip(np.rint(result), -32768, 32767).astype('<i2').tobytes()


def normalized_chunks(chunks: Iterator[Chunk]) -> Iterator[NormalizedFrame]:
    """Pull-based Phase 3 input: consumes ONLY durable chunks, bounded read buffers.

    Resampler state crosses recovery chunks, never an epoch/gap boundary. The
    caller obtains verified manifests from storage/recovery before passing them.
    No persistent consumer cursor/semantic segmentation is implemented in Phase 2.
    """
    normalizer = None
    previous = None
    origin = emitted = 0
    for chunk in chunks:
        discontinuity = previous is None or (
            chunk.session_id != previous.session_id or chunk.capture_epoch != previous.capture_epoch
            or chunk.sample_rate != previous.sample_rate or chunk.channels != previous.channels
            or abs(chunk.start_monotonic_ns - previous.end_monotonic_ns) > 100_000_000)
        if discontinuity:
            if normalizer is not None:
                tail = normalizer.process(b'', last=True)
                if tail:
                    yield NormalizedFrame(previous.session_id, previous.capture_epoch,
                                          origin + round(emitted / 16000 * 1e9), tail)
            normalizer = Normalizer(chunk.sample_rate, chunk.channels)
            origin, emitted = chunk.start_monotonic_ns, 0
        with wave.open(chunk.path, 'rb') as wav:
            while pcm := wav.readframes(4096):
                out = normalizer.process(pcm)
                if out:
                    yield NormalizedFrame(chunk.session_id, chunk.capture_epoch,
                                          origin + round(emitted / 16000 * 1e9), out)
                    emitted += len(out) // 2
        previous = chunk
    if normalizer is not None:
        tail = normalizer.process(b'', last=True)
        if tail:
            yield NormalizedFrame(previous.session_id, previous.capture_epoch,
                                  origin + round(emitted / 16000 * 1e9), tail)
