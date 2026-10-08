"""Reads replies aloud with Piper (optional: pip install piper-tts), on the CPU.

Voices are .onnx files (with their .onnx.json next to them) in the voices folder;
the "voice" setting picks one by name, otherwise the first one found is used.
Audio is played as it's made, one sentence at a time: through paplay or aplay on
Linux, winsound on Windows. A reply can also be spoken while it streams in: start(),
feed() it the text as it arrives, then finish()."""
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

try:
    from piper import PiperVoice, SynthesisConfig
except ImportError:
    PiperVoice = SynthesisConfig = None

LEAD_IN = 0.3  # seconds of silence before the first sentence


def speakable(text):
    """Reply text as it should be read: no code blocks, markdown marks or link URLs."""
    text = re.sub(r"<think>.*?(</think>|$)", " ", text, flags=re.S)
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # also an unclosed block
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*([-*_]\s*){3,}$", "", text, flags=re.M)  # horizontal rules
    text = re.sub(r"^\s*(#+|>+|[-*+])\s+", "", text, flags=re.M)  # headings, quotes, bullets
    text = re.sub(r"\*\*|__|~~|\*", "", text)
    return text.replace("|", " ").strip()


def split_ready(text):
    """Streamed reply text -> (whole sentences and lines that can be spoken now, the
    rest to wait on). Never cuts inside a code block or <think> block still open."""
    think = text.find("<think>")
    if think != -1 and "</think>" not in text:
        text, held = text[:think], text[think:]
    else:
        held = ""
    cut = 0
    for m in re.finditer(r"\n|[.!?][\"')\]]*\s", text):
        if text.count("```", 0, m.end()) % 2 == 0:
            cut = m.end()
    return text[:cut], text[cut:] + held


def raw_player(rate, channels):
    """Command that plays 16-bit PCM from stdin, or None if there's none (Windows)."""
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--format=s16le", f"--channels={channels}"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", str(channels)]
    return None


class Speaker:
    """Speaks one reply at a time in a background thread; stop() cuts it off."""

    def __init__(self, voice_dir):
        self.voice_dir = Path(voice_dir)
        self.voice = self.voice_file = None  # loaded voice, kept for the next reply
        self.lock = threading.Lock()  # one playback at a time
        self.stop_event = threading.Event()
        self.queue = queue.Queue()  # text still to speak; None ends the reply
        self.thread = None
        self.proc = None
        self.error = None

    def voice_path(self, name=None):
        """The chosen voice's .onnx file, or the first one in the folder; None if none."""
        if name and (self.voice_dir / f"{name}.onnx").is_file():
            return self.voice_dir / f"{name}.onnx"
        return next(iter(sorted(self.voice_dir.glob("*.onnx"))), None)

    def voices(self):
        """Names of the installed voices, e.g. en_US-lessac-medium."""
        return sorted(f.stem for f in self.voice_dir.glob("*.onnx"))

    def available(self):
        return PiperVoice is not None and self.voice_path() is not None

    @property
    def speaking(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def speak(self, text, voice=None, speed=1.0):
        self.start(voice, speed)
        self.feed(text)
        self.finish()

    def start(self, voice=None, speed=1.0):
        """Begin a reply (cutting off the one being spoken): feed() it, then finish().
        speed 2 talks twice as fast, 0.5 half as fast."""
        self.stop()
        self.error = None
        stop, q = self.stop_event, self.queue = threading.Event(), queue.Queue()
        self.thread = threading.Thread(
            target=self.run, args=(q, self.voice_path(voice), speed, stop), daemon=True
        )
        self.thread.start()

    def feed(self, text):
        self.queue.put(text)

    def finish(self):
        self.queue.put(None)

    def stop(self):
        self.stop_event.set()
        self.queue.put(None)  # wakes a thread waiting for more text
        p = self.proc
        if p is not None:
            p.kill()
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(None, 0)

    def run(self, q, path, speed, stop):
        with self.lock:  # waits for a stopped playback to wind down
            try:
                if path != self.voice_file:
                    self.voice, self.voice_file = PiperVoice.load(path), path
                config = SynthesisConfig(length_scale=1 / speed) if speed != 1 else None
                while (text := q.get()) is not None and not stop.is_set():
                    for chunk in self.voice.synthesize(speakable(text), config):
                        if stop.is_set():
                            break
                        self.play(chunk, stop)
                if self.proc is not None and not stop.is_set():
                    self.proc.stdin.close()
                    self.proc.wait()  # until the last sentence has been heard
            except Exception as e:
                if not stop.is_set():  # a killed player breaks the pipe; that's expected
                    self.error = str(e) or type(e).__name__
            finally:
                if self.proc is not None:
                    self.proc.kill()
                    try:
                        self.proc.stdin.close()
                    except OSError:  # unplayed audio left in the pipe
                        pass
                    self.proc.wait()
                    self.proc = None

    def play(self, chunk, stop):
        data = chunk.audio_int16_bytes
        if self.proc is None:
            cmd = raw_player(chunk.sample_rate, chunk.sample_channels)
            if cmd is None:
                return self.play_wav(chunk, data, stop)
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if stop.is_set():  # stop() came between the check and starting the player
                self.proc.kill()
            # a sound device waking from idle drops the start of a stream: let it drop silence
            frames = int(chunk.sample_rate * LEAD_IN)
            self.proc.stdin.write(bytes(frames * chunk.sample_width * chunk.sample_channels))
        self.proc.stdin.write(data)

    def play_wav(self, chunk, data, stop):
        """Windows: no player to pipe into, so each sentence becomes a short .wav."""
        import winsound
        path = Path(tempfile.gettempdir()) / "lulim-speech.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(chunk.sample_channels)
            w.setsampwidth(chunk.sample_width)
            w.setframerate(chunk.sample_rate)
            w.writeframes(data)
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        secs = len(data) / (chunk.sample_rate * chunk.sample_width * chunk.sample_channels)
        if stop.wait(secs):
            winsound.PlaySound(None, 0)
