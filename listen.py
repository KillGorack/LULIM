"""Dictation: records the mic until you stop talking, then turns it into text with
Whisper (optional: pip install faster-whisper), on the CPU.

Records with pw-record or arecord on Linux, otherwise with the sounddevice package
(Windows: pip install sounddevice). The end of what you said is found with the Silero
speech detector that comes with faster-whisper. The model ("small" unless the
"whisper_model" setting names another) is downloaded on first use into
~/.cache/huggingface, then kept loaded for the next message."""
import importlib.util
import queue
import shutil
import subprocess
import threading

RATE = 16000  # what Whisper expects: 16 kHz mono, 16-bit
MODEL = "small"
SILENCE = 1.5  # seconds of quiet after speech that end the recording
NO_SPEECH = 10  # give up if nothing is said for this long
MAX_SECS = 120
FRAME = 512  # samples per speech-detector frame (32 ms)
HAVE_WHISPER = importlib.util.find_spec("faster_whisper") is not None


def record_command():
    """Command that streams the default mic to stdout as raw PCM, or None if none."""
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(RATE), "--channels", "1", "--raw", "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"]
    return None


class Mic:
    """The default mic as a stream of raw PCM: read(n) bytes at a time."""

    def __init__(self):
        cmd = record_command()
        self.proc = self.stream = None
        if cmd:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        else:
            import sounddevice
            self.stream = sounddevice.RawInputStream(samplerate=RATE, channels=1, dtype="int16")
            self.stream.start()

    def read(self, n):
        if self.proc is not None:
            return self.proc.stdout.read(n)
        data, _ = self.stream.read(n // 2)
        return bytes(data)

    def close(self):
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait()
        else:
            self.stream.stop()
            self.stream.close()


class Listener:
    """One dictation at a time, in a background thread: listen(), then read events()
    as it goes: ("transcribing", None), then ("text", what was said, "" if nothing),
    ("error", message) or ("cancelled", None)."""

    def __init__(self):
        self.events = queue.Queue()
        self.thread = None
        self.finish_event = threading.Event()
        self.cancel_event = threading.Event()
        self.model = self.model_name = None  # loaded Whisper model, kept for next time
        self.vad = None

    def available(self):
        return HAVE_WHISPER and (
            record_command() is not None or importlib.util.find_spec("sounddevice") is not None
        )

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def listen(self, model=MODEL, silence=SILENCE):
        self.finish_event, self.cancel_event = threading.Event(), threading.Event()
        self.events = queue.Queue()
        self.thread = threading.Thread(
            target=self.run, args=(model, silence, self.finish_event, self.cancel_event),
            daemon=True,
        )
        self.thread.start()

    def finish(self):
        """Stop recording now and transcribe what's there."""
        self.finish_event.set()

    def cancel(self):
        self.cancel_event.set()
        self.finish_event.set()

    def run(self, model, silence, finish, cancel):
        try:
            pcm = self.record(silence, finish)
            if cancel.is_set():
                self.events.put(("cancelled", None))
                return
            if not pcm:
                self.events.put(("text", ""))
                return
            self.events.put(("transcribing", None))
            text = self.transcribe(pcm, model)
            self.events.put(("cancelled", None) if cancel.is_set() else ("text", text))
        except Exception as e:
            self.events.put(("error", str(e) or type(e).__name__))

    def record(self, silence, finish):
        """Mic audio until `silence` seconds of quiet follow speech, or finish is set.
        Returns the PCM, or b"" if no speech was heard."""
        import numpy as np
        from faster_whisper.vad import get_vad_model
        if self.vad is None:
            self.vad = get_vad_model()
        frame_secs = FRAME / RATE
        quiet_frames = int(silence / frame_secs)
        window = (quiet_frames + 16) * FRAME * 2  # bytes looked at in each check
        pcm = bytearray()
        heard = False
        mic = Mic()
        try:
            while not finish.is_set():
                data = mic.read(FRAME * 2 * 4)  # ~0.13 s
                if not data:
                    raise RuntimeError("the recorder stopped")
                pcm += data
                tail = pcm[-window:]
                tail = tail[len(tail) % (FRAME * 2):]  # whole frames only
                if not tail:
                    continue
                probs = self.vad(np.frombuffer(tail, np.int16).astype(np.float32) / 32768)
                heard = heard or probs.max() > 0.5
                secs = len(pcm) / (RATE * 2)
                if heard and len(probs) >= quiet_frames and probs[-quiet_frames:].max() < 0.35:
                    break
                if (not heard and secs > NO_SPEECH) or secs > MAX_SECS:
                    break
        finally:
            mic.close()
        return bytes(pcm) if heard else b""

    def transcribe(self, pcm, model):
        """What was said. The audio goes to Whisper as samples, not a file:
        faster-whisper's file reading breaks with av 19."""
        import numpy as np
        from faster_whisper import WhisperModel
        if model != self.model_name:
            self.model = WhisperModel(model, device="cpu", compute_type="int8")
            self.model_name = model
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        segments, _ = self.model.transcribe(audio, vad_filter=True)  # skips silence
        return " ".join(s.text.strip() for s in segments).strip()
