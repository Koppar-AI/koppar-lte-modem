"""Call audio through the board's USB sound card ("Koppar LTE modem").

Its output is what the caller hears; its input is what the caller says.
Needs the audio extra: pip install 'koppar-lte-modem[audio]' (sounddevice, numpy).

    from koppar_lte_modem.audio import CallAudio
    with CallAudio() as call:
        call.play(samples)              # float32 mono at call.rate
        heard = call.record(3.0)        # 3 s of the caller
"""

from __future__ import annotations

import queue
import threading
import wave

DEVICE_NAME = "Koppar"


class AudioError(Exception):
    pass


def _sd():
    try:
        import numpy  # noqa: F401
        import sounddevice
    except ImportError as e:
        raise AudioError("audio needs sounddevice and numpy: pip install 'koppar-lte-modem[audio]'") from e
    return sounddevice


def find_device() -> tuple[int, int, int]:
    """(input index, output index, sample rate) of the board's sound card."""
    sd = _sd()
    ins = outs = None
    rate = None
    for i, d in enumerate(sd.query_devices()):
        if DEVICE_NAME.lower() in d["name"].lower():
            if d["max_input_channels"] > 0 and ins is None:
                ins = i
            if d["max_output_channels"] > 0 and outs is None:
                outs = i
            rate = int(d["default_samplerate"])
    if ins is None or outs is None:
        raise AudioError("the Koppar sound card is not visible (is the modem firmware flashed and the board plugged in?)")
    return ins, outs, rate


def read_wav(path: str, rate: int):
    """Load a WAV as float32 mono, resampled to `rate`."""
    import numpy as np

    with wave.open(path, "rb") as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2**31
    else:
        raise AudioError(f"unsupported WAV sample width: {width} bytes")
    x = x.reshape(-1, ch).mean(axis=1)
    return resample(x, sr, rate)


def write_wav(path: str, x, rate: int) -> None:
    import numpy as np

    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def resample(x, src: int, dst: int):
    """Linear-interpolation resampler with a crude low-pass when going down;
    plenty for telephone-band speech."""
    import numpy as np

    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    if dst < src:
        k = int(round(src / dst))
        if k > 1:
            x = np.convolve(x, np.ones(k) / k, mode="same")
    n = int(round(len(x) * dst / src))
    t = np.arange(n) * (src / dst)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


class CallAudio:
    """Full-duplex access to the call: play into it, record from it."""

    def __init__(self, blocksize: int = 160):
        self.sd = _sd()
        self.inp, self.out, self.rate = find_device()
        self.blocksize = blocksize
        self._rx: queue.Queue = queue.Queue()
        self._tx = None
        self._tx_pos = 0
        self._tx_done = threading.Event()
        self._tx_done.set()
        self._lock = threading.Lock()
        self.stream = None

    def _callback(self, indata, outdata, frames, time_info, status):
        self._rx.put(indata[:, 0].copy())
        outdata.fill(0)
        with self._lock:
            if self._tx is not None:
                chunk = self._tx[self._tx_pos:self._tx_pos + frames]
                outdata[: len(chunk), 0] = chunk
                self._tx_pos += len(chunk)
                if self._tx_pos >= len(self._tx):
                    self._tx = None
                    self._tx_done.set()

    def __enter__(self):
        self.stream = self.sd.Stream(device=(self.inp, self.out), samplerate=self.rate, channels=1,
                                     dtype="float32", blocksize=self.blocksize, callback=self._callback)
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def play(self, samples, wait: bool = True) -> None:
        """Queue float32 mono samples at self.rate for the caller to hear."""
        import numpy as np

        with self._lock:
            self._tx = np.asarray(samples, dtype=np.float32)
            self._tx_pos = 0
            self._tx_done.clear()
        if wait:
            self._tx_done.wait()

    def play_wav(self, path: str, wait: bool = True) -> None:
        self.play(read_wav(path, self.rate), wait)

    def record(self, seconds: float):
        """The next `seconds` of what the caller says, as float32 mono."""
        import numpy as np

        while not self._rx.empty():          # drop what was buffered before the call
            self._rx.get_nowait()
        need = int(seconds * self.rate)
        parts, got = [], 0
        while got < need:
            blk = self._rx.get(timeout=2)
            parts.append(blk)
            got += len(blk)
        return np.concatenate(parts)[:need]

    def record_until(self, stop: threading.Event, max_seconds: float):
        """What the caller says until `stop` is set or `max_seconds` pass (voicemail)."""
        import numpy as np

        while not self._rx.empty():
            self._rx.get_nowait()
        need = int(max_seconds * self.rate)
        parts, got = [], 0
        while got < need and not stop.is_set():
            try:
                blk = self._rx.get(timeout=0.5)
            except queue.Empty:
                continue
            parts.append(blk)
            got += len(blk)
        return np.concatenate(parts)[:need] if parts else np.zeros(0, np.float32)


def play_file(path: str) -> None:
    sd = _sd()
    _, out, rate = find_device()
    sd.play(read_wav(path, rate), samplerate=rate, device=out, blocking=True)


def record_file(path: str, seconds: float) -> None:
    sd = _sd()
    inp, _, rate = find_device()
    x = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="float32", device=inp, blocking=True)
    write_wav(path, x[:, 0], rate)


def bridge(stop: threading.Event | None = None, tap=None) -> None:
    """Headset mode: the computer's default mic into the call, the call into
    its default speakers. Runs until `stop` is set (or Ctrl-C). `tap(direction,
    samples)`, if given, gets a copy of each block: "up" is what you say, "down"
    what the caller says (float32 mono at the call rate)."""
    sd = _sd()
    inp, out, rate = find_device()
    default_in, default_out = sd.default.device

    def through(direction):
        def cb(indata, outdata, frames, t, status):
            outdata[:] = indata
            if tap is not None:
                tap(direction, indata[:, 0].copy())
        return cb

    up = sd.Stream(device=(default_in, out), samplerate=rate, channels=1, dtype="float32", callback=through("up"))
    down = sd.Stream(device=(inp, default_out), samplerate=rate, channels=1, dtype="float32",
                     callback=through("down"))
    stop = stop or threading.Event()
    with up, down:
        try:
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass


def tone(freq: float, seconds: float, rate: int, level_db: float = -12.0):
    import numpy as np

    t = np.arange(int(seconds * rate)) / rate
    return (10 ** (level_db / 20) * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def level_db(x) -> float:
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    return 20 * np.log10(rms * np.sqrt(2)) if rms > 0 else -200.0   # dBFS of a sine with this RMS


def goertzel_db(x, freq: float, rate: int) -> float:
    """Level of one frequency, in dBFS for a sine."""
    import numpy as np

    n = len(x)
    if n == 0:
        return -200.0
    k = 2 * np.pi * freq / rate
    w = np.exp(-1j * k * np.arange(n))
    mag = abs(np.dot(x, w)) * 2 / n
    return 20 * np.log10(mag) if mag > 0 else -200.0


def loopback_test(board_cmd, seconds: float = 1.5) -> dict:
    """Play a tone into the sound card with the firmware in loopback mode and
    check it comes back. `board_cmd(cmd)` runs a control-port command."""
    sd = _sd()
    import numpy as np

    inp, out, rate = find_device()
    board_cmd("audio loopback")
    try:
        f = 440.0 if rate <= 8000 else 1000.0
        sig = np.concatenate([np.zeros(int(0.3 * rate), np.float32), tone(f, seconds, rate, -12.0),
                              np.zeros(int(0.3 * rate), np.float32)])
        rec = sd.playrec(sig, samplerate=rate, channels=1, dtype="float32", device=(inp, out), blocking=True)[:, 0]
    finally:
        board_cmd("audio pcm")
    # Latency: cross-correlate the onset.
    corr = np.correlate(rec, sig[: int(0.3 * rate) + int(0.2 * rate)], mode="valid")
    lag = int(np.argmax(np.abs(corr))) if len(corr) else 0
    body = rec[int(0.5 * rate): int((0.3 + seconds) * rate)]
    got = goertzel_db(body, f, rate)
    return {"rate": rate, "freq": f, "sent_dbfs": -12.0, "received_dbfs": round(got, 1),
            "latency_ms": round(1000 * lag / rate, 1), "pass": abs(got + 12.0) < 3.0}


def tone_test(board_cmd, seconds: float = 1.0) -> dict:
    """Record the firmware's own 1 kHz test tone from the sound card's input."""
    sd = _sd()

    inp, _, rate = find_device()
    board_cmd("audio tone")
    try:
        rec = sd.rec(int((seconds + 0.3) * rate), samplerate=rate, channels=1, dtype="float32", device=inp,
                     blocking=True)[:, 0]
    finally:
        board_cmd("audio pcm")
    body = rec[int(0.3 * rate):]
    got = goertzel_db(body, 1000.0, rate)
    return {"rate": rate, "expected_dbfs": -12.0, "received_dbfs": round(got, 1),
            "total_dbfs": round(level_db(body), 1), "pass": abs(got + 12.0) < 2.0}
