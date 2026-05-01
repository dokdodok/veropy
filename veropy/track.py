from __future__ import annotations

import asyncio
from fractions import Fraction
import math
import time

import av
from aiortc import AudioStreamTrack
from av import AudioFrame
from av.audio.fifo import AudioFifo
from av.audio.resampler import AudioResampler


VOX_STREAM_ID = "ARDAMS"
VOX_TRACK_ID = "VOXMSa0"

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960
CHANNELS = 2
SAMPLE_WIDTH = 2


class SilentAudioTrack(AudioStreamTrack):
    def __init__(self) -> None:
        super().__init__()
        self._sample_index = 0

    async def recv(self) -> AudioFrame:
        frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._sample_index
        frame.time_base = Fraction(1, SAMPLE_RATE)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))

        self._sample_index += SAMPLES_PER_FRAME
        return frame


class SineAudioTrack(AudioStreamTrack):
    def __init__(
        self,
        *,
        frequency: float = 440.0,
        amplitude: float = 0.2,
    ) -> None:
        super().__init__()
        self._sample_index = 0
        self._start = time.monotonic()
        self.frequency = frequency
        self.amplitude = amplitude

    async def recv(self) -> AudioFrame:
        target_time = self._start + self._sample_index / SAMPLE_RATE
        wait = target_time - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        pcm = bytearray(SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH)
        for i in range(SAMPLES_PER_FRAME):
            sample_offset = self._sample_index + i
            sample = int(
                math.sin(2 * math.pi * self.frequency * sample_offset / SAMPLE_RATE)
                * self.amplitude
                * 32767
            )
            offset = i * CHANNELS * SAMPLE_WIDTH
            sample_bytes = sample.to_bytes(
                SAMPLE_WIDTH,
                "little",
                signed=True,
            )
            pcm[offset : offset + SAMPLE_WIDTH] = sample_bytes
            pcm[offset + SAMPLE_WIDTH : offset + (2 * SAMPLE_WIDTH)] = sample_bytes

        frame.planes[0].update(bytes(pcm))
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._sample_index
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._sample_index += SAMPLES_PER_FRAME
        return frame


class AudioSourceTrack(AudioStreamTrack):
    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.get_running_loop()
        self._sample_index = 0
        self._start = time.monotonic()
        self._frames: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=200)
        self._sources: asyncio.Queue[str] = asyncio.Queue()
        self._worker = asyncio.create_task(self._decode_loop())

    def add_source(self, source: str) -> None:
        self._sources.put_nowait(source)

    async def recv(self) -> AudioFrame:
        target_time = self._start + self._sample_index / SAMPLE_RATE
        wait = target_time - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            frame = self._frames.get_nowait()
        except asyncio.QueueEmpty:
            frame = self._create_silent_frame()

        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._sample_index
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._sample_index += frame.samples
        return frame

    def stop(self) -> None:
        self._worker.cancel()
        super().stop()

    async def _decode_loop(self) -> None:
        while True:
            source = await self._sources.get()
            try:
                await asyncio.to_thread(self._decode_source, source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("audio decode failed", source, repr(exc))

    def _decode_source(self, source: str) -> None:
        resampler = AudioResampler(
            format="s16",
            layout="stereo",
            rate=SAMPLE_RATE,
        )
        fifo = AudioFifo()

        with av.open(source, timeout=10.0) as container:
            stream = next(
                stream for stream in container.streams if stream.type == "audio"
            )
            for packet in container.demux(stream):
                for decoded_frame in packet.decode():
                    assert isinstance(decoded_frame, AudioFrame)
                    for frame in resampler.resample(decoded_frame):
                        frame.pts = None
                        fifo.write(frame)
                        self._drain_fifo(fifo)

            for frame in resampler.resample(None):
                frame.pts = None
                fifo.write(frame)
                self._drain_fifo(fifo)

    def _drain_fifo(self, fifo: AudioFifo) -> None:
        while fifo.samples >= SAMPLES_PER_FRAME:
            frame = fifo.read(SAMPLES_PER_FRAME)
            assert isinstance(frame, AudioFrame)

            future = asyncio.run_coroutine_threadsafe(
                self._frames.put(frame),
                self._loop,
            )
            future.result()

    def _create_silent_frame(self) -> AudioFrame:
        frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        return frame
