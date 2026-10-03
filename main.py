#!/usr/bin/env python3
import base64
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import warnings
import tkinter as tk
from tkinter import filedialog
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import requests
from PIL import Image

from render import ReplyRenderer

MODEL = "qwen2.5-coder:7b"
URL = "http://localhost:11434/api/chat"
UNLOAD_URL = "http://localhost:11434/api/generate"
TAGS_URL = "http://localhost:11434/api/tags"
SHOW_URL = "http://localhost:11434/api/show"
# context window to ask Ollama for (its own default is small, so long chats lose
# their start); capped at what the model supports. Override with "num_ctx" in settings.json
NUM_CTX = 16384
CHAT_DIR = Path.home() / ".local/share/quen/chats"
SETTINGS_FILE = CHAT_DIR.parent / "settings.json"
IMAGE_DIR = CHAT_DIR / "images"  # attached images, named by content hash
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
IMAGE_MAX = 1600  # longest side kept when attaching; models scale down further anyway
ICON_DIR = Path(__file__).with_name("icons")  # Lucide icons (ISC), see icons/LICENSE

THEMES = {
    "dark": {
        "window": "#262626",
        "surface": "#171717",
        "text": "#b1b1b1",
        "bubble": "#2e2e2e",
        "bubble_text": "#b1b1b1",
        "ai_text": "#d4d4d4",
        "error": "#f87171",
        "code_bg": "#262626",
        "code_head": "#9a9a9a",
        "code_head_bg": "#333333",
        "muted": "#8f8f8f",
        "link": "#8ab4f8",
        "rule": "#3a3a3a",
        "button": "#525252",
        "button_hover": "#737373",
        "list_hover": "#333333",
        "scrollbar": "#404040",
        "scrollbar_hover": "#525252",
        "syn_keyword": "#c09bc8",
        "syn_string": "#a3b88a",
        "syn_comment": "#767676",
        "syn_number": "#d1a173",
        "syn_builtin": "#80b3ad",
        "syn_func": "#8eaed4",
    },
    "light": {
        "window": "#bdb5a3",
        "surface": "#ded6c3",
        "text": "#332d24",
        "bubble": "#c9c0aa",
        "bubble_text": "#252118",
        "ai_text": "#252118",
        "error": "#a33a2a",
        "code_bg": "#d1c8b2",
        "code_head": "#5c5344",
        "code_head_bg": "#c3b9a1",
        "muted": "#6b6252",
        "link": "#2f5d8a",
        "rule": "#b3a990",
        "button": "#8c806b",
        "button_hover": "#756a58",
        "list_hover": "#cbc2ae",
        "scrollbar": "#a89e8a",
        "scrollbar_hover": "#8c806b",
        "syn_keyword": "#7a3d6c",
        "syn_string": "#4d6a2c",
        "syn_comment": "#7d725e",
        "syn_number": "#8a5220",
        "syn_builtin": "#2c6863",
        "syn_func": "#2f5d8a",
    },
}

FONT_SIZE = 13  # chat and input text; Ctrl +/- change it, Ctrl+0 resets
FONT_MIN, FONT_MAX = 9, 24


def strip_think(text):
    """Reply text without <think> blocks (older setups put reasoning in the content)."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S)


def store_image(data):
    """Save image bytes as a PNG in IMAGE_DIR; returns its file name, or None if the
    bytes aren't an image. Same image twice -> same file."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    img.thumbnail((IMAGE_MAX, IMAGE_MAX))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = buf.getvalue()
    name = hashlib.sha1(png).hexdigest()[:16] + ".png"
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not (IMAGE_DIR / name).exists():
        (IMAGE_DIR / name).write_bytes(png)
    return name


def thumbnail(names, height):
    """Tk image of the stored images side by side, at most `height` px tall."""
    imgs = []
    for name in names:
        try:
            im = Image.open(IMAGE_DIR / name).convert("RGBA")
        except OSError:
            continue  # file gone: just leave it out
        im.thumbnail((height * 3, height))
        imgs.append(im)
    if not imgs:
        return None
    gap = 6
    strip = Image.new("RGBA", (sum(i.width for i in imgs) + gap * (len(imgs) - 1),
                               max(i.height for i in imgs)), (0, 0, 0, 0))
    x = 0
    for im in imgs:
        strip.paste(im, (x, 0))
        x += im.width + gap
    buf = io.BytesIO()
    strip.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


def clipboard_image():
    """Image on the clipboard as bytes, or None. Reads it with wl-paste (Tk can't
    get images from the Wayland clipboard). A copied image file counts too."""
    if not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("wl-paste"):
        return None
    try:
        run = lambda *a: subprocess.run(["wl-paste", *a], capture_output=True, timeout=5).stdout
        types = run("--list-types").decode().split()
        kind = next((t for t in types if t.startswith("image/")), None)
        if kind:
            return run("--type", kind)
        if "text/uri-list" in types:  # e.g. a file copied in the file manager
            for uri in run("--type", "text/uri-list").decode().split():
                path = Path(requests.utils.unquote(uri.removeprefix("file://")))
                if path.suffix.lower() in IMAGE_TYPES and path.is_file():
                    return path.read_bytes()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        pass
    return None


def desktop_file_dialog(save, title, start, label, patterns):
    """The desktop's own file dialog (kdialog or zenity): open several files, or save
    one (`start` is then the suggested path). Returns the chosen paths, [] if cancelled."""
    pattern = " ".join(patterns)
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--title", title,
               "--getsavefilename" if save else "--getopenfilename", str(start), f"{label} ({pattern})"]
        if not save:
            cmd += ["--multiple", "--separate-output"]
    else:
        cmd = ["zenity", "--file-selection", f"--title={title}", f"--file-filter={label} | {pattern}"]
        cmd += ["--save", f"--filename={start}"] if save else ["--multiple", "--separator=\n"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return [p for p in r.stdout.splitlines() if p]


def load_icon(name, color, size=18):
    """Load a Lucide icon from icons/ as a Tk image, tinted to the given color.

    The PNGs are white masks; only their alpha is used. Built as PNG data for
    tk.PhotoImage so it doesn't need PIL.ImageTk (a separate distro package on Fedora).
    """
    mask = Image.open(ICON_DIR / f"{name}.png").convert("RGBA").getchannel("A")
    mask = mask.resize((size, size), Image.LANCZOS)
    img = Image.new("RGBA", (size, size), color)
    img.putalpha(mask)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


class MyApp(ctk.CTk):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.geometry("800x500")
        self.title("LULIM")
        self.settings = self.load_settings()
        self.font_size = self.settings.get("font_size", FONT_SIZE)
        self.theme_name = self.settings.get("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"

        self.icon = tk.PhotoImage(file=Path(__file__).with_name("icon.png"))
        self.iconphoto(True, self.icon)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.response = ctk.CTkTextbox(
            self,
            wrap="word",
            state="disabled",
            font=("TkDefaultFont", self.font_size),
        )
        self.response.grid(row=0, column=1, sticky="nsew", padx=10, pady=(10, 0))
        tb = self.response._textbox
        tb.configure(padx=8, pady=6)
        # user bubbles sit on their own right-justified line
        tb.tag_config("user_line", justify="right", spacing1=14, spacing3=12, rmargin=2)
        self.renderer = ReplyRenderer(self.response, self.font_size)
        tb.bind("<Configure>", lambda e: self.rewrap_bubbles(), add="+")
        self.bubbles = []

        self.entry = ctk.CTkTextbox(
            self,
            height=70,
            wrap="word",
            font=("TkDefaultFont", self.font_size),
        )
        self.entry.grid(row=3, column=1, sticky="ew", padx=10, pady=(0, 10))
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Control-v>", self.on_paste)
        self.entry.bind("<Shift-Return>", self.on_shift_enter)
        self.bind("<Escape>", lambda e: self.stop())

        # images waiting to be sent, above the entry; hidden while there are none
        self.pending = []
        self.attach_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.attach_bar.grid(row=2, column=1, sticky="ew", padx=10, pady=(0, 10))
        self.attach_bar.grid_remove()

        # only shown while a reply is streaming, over the right edge of the entry
        self.stop_btn = ctk.CTkButton(self.entry, text="Stop", width=60, command=self.stop)

        # button bar between chat and entry: sidebar toggle and new chat on the left,
        # model and theme on the right
        self.toolbar = ctk.CTkFrame(self, corner_radius=self.response.cget("corner_radius"))
        self.toolbar.grid(row=1, column=1, sticky="ew", padx=10, pady=10)
        self.toolbar.grid_columnconfigure(2, weight=1)

        self.sidebar_btn = ctk.CTkButton(
            self.toolbar, text="", width=32, command=self.toggle_sidebar
        )
        self.sidebar_btn.grid(row=0, column=0, padx=(6, 2), pady=6)
        self.new_btn = ctk.CTkButton(self.toolbar, text="", width=32, command=self.new_chat)
        self.new_btn.grid(row=0, column=1, pady=6)

        # shown in the bar's empty middle while Ollama can't be reached
        self.status = ctk.CTkLabel(self.toolbar, text="", anchor="e")
        self.status.grid(row=0, column=2, sticky="e", padx=8)
        self.note = self.note_job = None  # brief message shown there, e.g. text size

        # attach images: only shown when the current model can see them
        self.attach_btn = ctk.CTkButton(self.toolbar, text="", width=32, command=self.attach_dialog)
        self.attach_btn.grid(row=0, column=3, padx=(0, 4), pady=6)
        self.model_infos = {}  # model -> {"ctx": trained context length, "vision": bool}

        models = self.get_models()
        self.online = True
        self.set_online(models is not None)
        models = models or [MODEL]
        self.model = self.pick_model(models)
        self.model_menu = ctk.CTkOptionMenu(
            self.toolbar,
            values=models,
            width=200,
            command=self.set_model,
        )
        self.model_menu.set(self.model)
        self.title(f"LULIM - {self.model}")
        self.model_menu.grid(row=0, column=4, padx=(0, 4), pady=6)

        self.prompt_btn = ctk.CTkButton(
            self.toolbar, text="", width=32, command=self.edit_system_prompt
        )
        self.prompt_btn.grid(row=0, column=5, padx=(0, 2), pady=6)
        self.prompt_win = None

        self.theme_btn = ctk.CTkButton(
            self.toolbar,
            text="",
            width=32,
            command=self.toggle_theme,
        )
        self.theme_btn.grid(row=0, column=6, padx=(0, 6), pady=6)

        # sidebar: just the saved chats
        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color="transparent")
        self.sidebar.grid(row=0, column=0, rowspan=4, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(0, weight=1)
        self.sidebar.grid_columnconfigure(0, weight=1)

        self.container = ctk.CTkFrame(
            self.sidebar,
            corner_radius=self.response.cget("corner_radius"),
        )
        self.container.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(1, weight=1)

        # filters the list as you type: every word has to appear in the name or the chat
        self.search = ctk.CTkEntry(self.container, placeholder_text="Search chats", border_width=0)
        self.search.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
        self.search_job = None
        self.search.bind("<KeyRelease>", self.on_search_key)
        self.search.bind("<Escape>", lambda e: self.clear_search())

        self.chat_list = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self.chat_list.grid(row=1, column=0, sticky="nsew", padx=5, pady=(4, 10))
        self.chat_list.grid_columnconfigure(0, weight=1)
        self.chat_buttons = []
        self.chat_list._parent_canvas.bind(
            "<Configure>", lambda e: self.after_idle(self.list_resized), add="+"
        )
        self.bind("<Control-b>", lambda e: self.toggle_sidebar())
        self.bind("<Control-f>", lambda e: self.focus_search())
        self.bind("<Control-n>", lambda e: self.new_chat())
        for key, step in (("plus", 1), ("equal", 1), ("KP_Add", 1), ("minus", -1),
                          ("KP_Subtract", -1), ("Key-0", 0)):
            self.bind(f"<Control-{key}>", lambda e, s=step: self.zoom(s))

        self.history = []
        self.q = queue.Queue()
        self.generating = False
        self.stop_event = threading.Event()
        self.resp = None
        self.chat_id = None
        self.armed = self.armed_btn = None  # chat whose trash can was clicked once
        self.closing = False

        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        self.cleanup_images()  # e.g. attached but never sent before the app closed
        self.apply_theme()
        self.update_attach_btn()
        if self.settings.get("sidebar_hidden"):
            self.sidebar.grid_remove()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def get_models(self):
        """Installed model names, or None if Ollama can't be reached."""
        try:
            r = requests.get(TAGS_URL, timeout=3)
            return [m["name"] for m in r.json()["models"]]
        except Exception:
            return None

    def pick_model(self, models):
        last = self.settings.get("model", MODEL)
        return last if last in models else (MODEL if MODEL in models else models[0])

    def set_online(self, online):
        """Show or clear the "not reachable" notice; while offline, check again every 5s."""
        was = self.online
        self.online = online
        self.update_status()
        if was and not online:
            self.after(5000, self.check_ollama)

    def update_status(self):
        """The toolbar notice: a brief note wins over "Ollama not reachable"."""
        t = THEMES[self.theme_name]
        if self.note:
            self.status.configure(text=self.note, text_color=t["muted"])
        else:
            self.status.configure(text="" if self.online else "Ollama not reachable",
                                  text_color=t["error"])

    def show_note(self, text, ms=2000):
        if self.note_job:
            self.after_cancel(self.note_job)
        self.note = text
        self.update_status()

        def clear():
            self.note = self.note_job = None
            self.update_status()

        self.note_job = self.after(ms, clear)

    def check_ollama(self):
        if self.online:
            return
        models = self.get_models()
        if models is None:
            self.after(5000, self.check_ollama)
            return
        # it's up: reload the model list, which was only a placeholder
        models = models or [MODEL]
        self.model = self.pick_model(models)
        self.model_menu.configure(values=models)
        self.model_menu.set(self.model)
        self.title(f"LULIM - {self.model}")
        self.set_online(True)
        self.update_attach_btn()

    def model_info(self, model):
        """What Ollama reports about a model: {"ctx": trained context length or None,
        "vision": can it read images}. Cached; None while Ollama can't be reached."""
        if model not in self.model_infos:
            try:
                r = requests.post(SHOW_URL, json={"model": model}, timeout=5)
                data = r.json()
            except Exception:
                return None  # don't remember "unknown": ask again next time
            info = data.get("model_info", {})
            self.model_infos[model] = {
                "ctx": next((v for k, v in info.items() if k.endswith(".context_length")), None),
                "vision": "vision" in data.get("capabilities", []),
            }
        return self.model_infos[model]

    def has_vision(self):
        info = self.model_info(self.model)
        return bool(info and info["vision"])

    def update_attach_btn(self):
        if self.has_vision():
            self.attach_btn.grid()
        else:
            self.attach_btn.grid_remove()

    def num_ctx(self):
        """Context window for the current model: the setting, capped at the model's max."""
        want = self.settings.get("num_ctx", NUM_CTX)
        info = self.model_info(self.model)
        limit = info and info["ctx"]
        return min(want, limit) if limit else want

    def load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_setting(self, key, value):
        self.settings[key] = value
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings))
        except OSError:
            pass

    def set_model(self, choice):
        self.model = choice
        self.title(f"LULIM - {choice}")
        self.save_setting("model", choice)
        self.update_attach_btn()

    def toggle_sidebar(self):
        hidden = self.sidebar.winfo_manager() == ""
        if hidden:
            self.sidebar.grid()
        else:
            self.sidebar.grid_remove()
        self.save_setting("sidebar_hidden", not hidden)

    def zoom(self, step):
        """Text size of the chat and input box: +1/-1, or 0 to reset."""
        size = FONT_SIZE if step == 0 else min(FONT_MAX, max(FONT_MIN, self.font_size + step))
        if size == FONT_SIZE:
            self.show_note(f"Text size {size} (normal)")
        else:
            limit = " (largest)" if size == FONT_MAX else " (smallest)" if size == FONT_MIN else ""
            self.show_note(f"Text size {size}{limit}  ·  Ctrl+0 resets to {FONT_SIZE}")
        if size == self.font_size:
            return
        self.font_size = size
        self.save_setting("font_size", size)
        font = ("TkDefaultFont", size)
        self.response.configure(font=font)
        self.entry.configure(font=font)
        for b in self.bubbles:
            b.configure(font=font)
        self.renderer.set_size(size)
        self.rewrap_bubbles()

    def toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.save_setting("theme", self.theme_name)
        self.apply_theme()

    def apply_theme(self):
        t = THEMES[self.theme_name]
        self.t = t
        ctk.set_appearance_mode(self.theme_name)
        self.configure(fg_color=t["window"])
        for box in (self.response, self.entry):
            box.configure(
                fg_color=t["surface"],
                text_color=t["text"],
                scrollbar_button_color=t["scrollbar"],
                scrollbar_button_hover_color=t["scrollbar_hover"],
            )
        self.renderer.apply_theme(t)
        for b in self.bubbles:
            b.configure(fg_color=t["bubble"], text_color=t["bubble_text"], bg_color=t["surface"])
        self.container.configure(fg_color=t["surface"])
        self.update_status()
        self.search.configure(
            fg_color=t["list_hover"],
            text_color=t["text"],
            placeholder_text_color=t["muted"],
        )
        self.chat_list.configure(
            fg_color=t["surface"],
            scrollbar_button_color=t["scrollbar"],
            scrollbar_button_hover_color=t["scrollbar_hover"],
        )
        self.toolbar.configure(fg_color=t["surface"])
        # flat icon buttons on the bar: just the icon, highlighted on hover
        for btn in (self.sidebar_btn, self.new_btn, self.attach_btn, self.prompt_btn,
                    self.theme_btn):
            btn.configure(fg_color="transparent", hover_color=t["list_hover"])
        self.stop_btn.configure(
            fg_color=t["button"], hover_color=t["button_hover"], text_color=t["text"]
        )
        self.stop_btn.configure(bg_color=t["surface"])  # rounded corners sit on the entry
        # quiet, one-tone pill like the flat icon buttons around it
        self.model_menu.configure(
            fg_color=t["list_hover"],
            button_color=t["list_hover"],
            button_hover_color=t["scrollbar"],
            text_color=t["text"],
            dropdown_fg_color=t["surface"],
            dropdown_hover_color=t["list_hover"],
            dropdown_text_color=t["text"],
        )
        # the theme button shows the theme you'd switch to: sun while dark, moon while light
        theme = "sun" if self.theme_name == "dark" else "moon"
        size = round(18 * self.theme_btn._get_widget_scaling())
        self._icons = {}  # keep references so Tk doesn't drop the images
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # plain Tk images: we scale them ourselves
            for btn, name in ((self.sidebar_btn, "panel-left"), (self.new_btn, "square-pen"),
                              (self.attach_btn, "paperclip"), (self.prompt_btn, "scroll-text"),
                              (self.theme_btn, theme)):
                self._icons[name] = load_icon(name, t["text"], size)
                btn.configure(image=self._icons[name])
        self.refresh_attachments()
        list_size = round(16 * self.theme_btn._get_widget_scaling())
        self._icons["trash-2"] = load_icon("trash-2", t["muted"], list_size)
        self._icons["trash-armed"] = load_icon("trash-2", t["error"], list_size)
        self.refresh_list()

    def close(self):
        if self.generating:
            # stop the reply first; poll() calls close() again once it's saved
            self.closing = True
            self.stop()
            return
        try:
            requests.post(
                UNLOAD_URL,
                json={"model": self.model, "keep_alive": 0},
                timeout=5,
            )
        except requests.RequestException:
            pass
        self.destroy()

    def on_enter(self, event):
        self.send()
        return "break"

    def on_shift_enter(self, event):
        self.entry.insert("insert", "\n")
        return "break"

    def send(self):
        text = self.entry.get("1.0", "end").strip()
        if self.generating or not (text or self.pending):
            return
        if self.pending and not self.has_vision():
            self.show_note(f"{self.model} can't read images: remove them or switch models", 4000)
            return
        self.entry.delete("1.0", "end")
        msg = {"role": "user", "content": text}
        if self.pending:
            msg["images"], self.pending = self.pending, []
            self.refresh_attachments()
        self.history.append(msg)
        self.renderer.drop_actions("Regenerate", "Retry")  # only for the latest reply
        self.add_user_bubble(text, len(self.history) - 1, msg.get("images"))
        self.start_reply()

    def start_reply(self):
        """Ask the model to answer the history as it stands."""
        self.generating = True
        self.stop_event.clear()
        self.stop_btn.place(relx=1, rely=0.5, anchor="e", x=-22)
        self.renderer.start()
        self.stats = None
        threading.Thread(target=self.worker, daemon=True).start()
        self.after(50, self.poll)

    def regenerate(self):
        """Drop the latest reply (if any; after an error there is none) and ask again."""
        if self.generating or not self.history:
            return
        if self.history[-1]["role"] == "assistant":
            self.history.pop()
        self.render_history()
        self.start_reply()

    def reply_actions(self, i):
        """Links under the reply at history[i]: Copy, and Regenerate on the latest one."""
        actions = [("Copy", lambda: self.copy_reply(i), "Copied")]
        if i == len(self.history) - 1:
            actions.append(("Regenerate", self.regenerate, None))
        return actions

    def copy_reply(self, i):
        self.clipboard_clear()
        self.clipboard_append(strip_think(self.history[i]["content"]).strip())

    def stop(self):
        if not self.generating:
            return
        self.stop_event.set()
        # closing the stream unblocks the worker and makes Ollama abort the generation
        r = self.resp
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    def payload_messages(self):
        """History as sent to the model: no saved thinking, stats or <think> blocks.
        The system prompt goes first; it isn't saved with the chat, so editing it
        also applies to old chats when you continue them."""
        system = self.settings.get("system_prompt", "").strip()
        messages = [{"role": "system", "content": system}] if system else []
        # a model that can't see images gets just the text (e.g. after switching models)
        vision = any(m.get("images") for m in self.history) and self.has_vision()
        for m in self.history:
            msg = {"role": m["role"], "content": strip_think(m["content"])}
            if vision and m.get("images"):
                msg["images"] = [base64.b64encode((IMAGE_DIR / n).read_bytes()).decode()
                                 for n in m["images"] if (IMAGE_DIR / n).is_file()]
            messages.append(msg)
        return messages

    def on_paste(self, event):
        """Ctrl+V: an image on the clipboard gets attached; text pastes as usual."""
        data = clipboard_image()
        if data is None:
            return None
        if not self.has_vision():
            self.show_note(f"{self.model} can't read images", 3000)
        else:
            self.attach(data)
        return "break"

    def file_dialog(self, then, save, title, start, label, patterns):
        """Open/save dialog, then then(paths). The desktop's own dialog runs in a thread
        so the window keeps drawing (and a streaming reply keeps coming) meanwhile;
        without kdialog or zenity it falls back to Tk's basic one."""
        if shutil.which("kdialog") is None and shutil.which("zenity") is None:
            types = [(label, " ".join(patterns))]
            if save:
                p = filedialog.asksaveasfilename(parent=self, title=title, initialdir=start.parent,
                                                 initialfile=start.name, filetypes=types)
                then([p] if p else [])
            else:
                then(list(filedialog.askopenfilenames(parent=self, title=title, initialdir=start,
                                                      filetypes=types)))
            return
        result = []
        t = threading.Thread(daemon=True, target=lambda: result.extend(
            desktop_file_dialog(save, title, start, label, patterns)))
        t.start()

        def wait():
            if t.is_alive():
                self.after(100, wait)
            else:
                then(result)

        wait()

    def attach_dialog(self):
        """Paperclip button: pick image files to attach."""
        def attach_files(paths):
            for p in paths:
                try:
                    self.attach(Path(p).read_bytes())
                except OSError:
                    pass

        self.file_dialog(attach_files, False, "Attach images", Path.home(), "Images",
                         ["*" + e for e in IMAGE_TYPES])

    def attach(self, data):
        name = store_image(data)
        if name is None:
            self.show_note("That isn't an image Pillow can read", 3000)
            return
        if name not in self.pending:
            self.pending.append(name)
        self.refresh_attachments()

    def refresh_attachments(self):
        """Thumbnails of the images waiting to be sent, each with a remove button."""
        for w in self.attach_bar.winfo_children():
            w.destroy()
        if not self.pending:
            self.attach_bar.grid_remove()
            return
        t = self.t
        height = round(56 * self.attach_btn._get_widget_scaling())
        self._pending_thumbs = []  # keep references so Tk doesn't drop the images
        for name in self.pending:
            thumb = thumbnail([name], height)
            if thumb is None:
                continue
            self._pending_thumbs.append(thumb)
            cell = tk.Label(self.attach_bar, image=thumb, bd=0, bg=t["window"])
            cell.pack(side="left", padx=(0, 8))
            remove = ctk.CTkButton(
                cell, text="×", width=20, height=20, corner_radius=10,
                fg_color=t["button"], hover_color=t["button_hover"], text_color=t["text"],
                command=lambda n=name: self.unattach(n),
            )
            remove.place(relx=1, x=-2, y=2, anchor="ne")
        self.attach_bar.grid()

    def unattach(self, name):
        if name in self.pending:
            self.pending.remove(name)
        self.refresh_attachments()

    def cleanup_images(self):
        """Delete image files no saved chat (or pending message) uses any more."""
        if not IMAGE_DIR.is_dir():
            return
        used = set(self.pending)
        for m in self.history:
            used.update(m.get("images", []))
        for f in CHAT_DIR.glob("*.json"):
            try:
                for m in json.loads(f.read_text())["messages"]:
                    used.update(m.get("images", []))
            except (OSError, ValueError, KeyError):
                return  # can't tell what's used: keep everything
        for f in IMAGE_DIR.glob("*.png"):
            if f.name not in used:
                f.unlink(missing_ok=True)

    def edit_system_prompt(self):
        """Small window to edit the system prompt (one for all chats)."""
        if self.prompt_win is not None and self.prompt_win.winfo_exists():
            self.prompt_win.focus()
            return
        t = self.t
        win = self.prompt_win = ctk.CTkToplevel(self, fg_color=t["window"])
        win.title("System prompt")
        win.geometry("520x320")
        win.transient(self)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            win,
            text="Instructions sent to the model before every chat.",
            anchor="w",
            text_color=t["muted"],
        ).grid(row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=(10, 4))

        box = ctk.CTkTextbox(
            win,
            wrap="word",
            font=("TkDefaultFont", self.font_size),
            fg_color=t["surface"],
            text_color=t["text"],
            scrollbar_button_color=t["scrollbar"],
            scrollbar_button_hover_color=t["scrollbar_hover"],
        )
        box.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10)
        box.insert("1.0", self.settings.get("system_prompt", ""))

        def save():
            self.save_setting("system_prompt", box.get("1.0", "end").strip())
            win.destroy()

        style = dict(width=80, fg_color=t["button"], hover_color=t["button_hover"],
                     text_color=t["text"])
        ctk.CTkButton(win, text="Cancel", command=win.destroy, **style).grid(
            row=2, column=1, padx=(0, 6), pady=10)
        ctk.CTkButton(win, text="Save", command=save, **style).grid(
            row=2, column=2, padx=(0, 10), pady=10)
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Control-Return>", lambda e: save())
        # CTkToplevel finishes setting itself up after a moment; focus once it's shown
        win.after(150, lambda: (win.focus(), box.focus()))

    def worker(self):
        reply = thinking = ""
        stats = None
        try:
            ctx = self.num_ctx()
            with requests.post(
                URL,
                json={
                    "model": self.model,
                    "messages": self.payload_messages(),
                    "stream": True,
                    "options": {"num_ctx": ctx},
                },
                stream=True,
                timeout=300,
            ) as r:
                self.resp = r
                r.raise_for_status()
                for line in r.iter_lines():
                    if self.stop_event.is_set():
                        break
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message", {})
                    if msg.get("thinking"):
                        thinking += msg["thinking"]
                        self.q.put(("think", msg["thinking"]))
                    if msg.get("content"):
                        reply += msg["content"]
                        self.q.put(("token", msg["content"]))
                    if chunk.get("done"):
                        n, ns = chunk.get("eval_count"), chunk.get("eval_duration")
                        if n and ns:
                            stats = {"tokens": n, "tps": round(n / (ns / 1e9), 1)}
                            prompt = chunk.get("prompt_eval_count")
                            if prompt:
                                stats["ctx"] = prompt + n
                                stats["ctx_max"] = ctx
                        break
        except requests.ConnectionError:
            if not self.stop_event.is_set():
                self.q.put(("error", "[error: can't reach Ollama at localhost:11434. Is it running?]"))
                self.q.put(("offline", None))
        except Exception as e:
            if not self.stop_event.is_set():
                self.q.put(("error", f"[error: {e}]"))
        self.resp = None
        stopped = self.stop_event.is_set()
        if reply:
            entry = {"role": "assistant", "content": reply}
            if stopped:
                entry["stopped"] = True
            if thinking:
                entry["thinking"] = thinking
            if stats:
                entry["stats"] = stats
            self.history.append(entry)
        self.q.put(("done", (stats, stopped)))

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "token":
                    self.renderer.feed(data)
                elif kind == "think":
                    self.renderer.think(data)
                elif kind == "error":
                    self.renderer.show_error(data)
                elif kind == "offline":
                    self.set_online(False)
                elif kind == "done":
                    stats, stopped = data
                    if self.history and self.history[-1]["role"] == "assistant":
                        actions = self.reply_actions(len(self.history) - 1)
                    else:  # no reply: failed (e.g. Ollama down) or stopped before any text
                        actions = [("Retry", self.regenerate, None)]
                    self.renderer.finish(stats=stats, stopped=stopped, actions=actions)
                    secs = self.renderer.think_secs
                    if secs is not None and self.history and self.history[-1]["role"] == "assistant":
                        self.history[-1]["think_secs"] = secs
                    self.generating = False
                    self.stop_btn.place_forget()
                    self.save_chat()
                    if self.closing:
                        self.close()
                    return
        except queue.Empty:
            pass
        self.after(50, self.poll)

    def clear_response(self):
        self.response.configure(state="normal")
        self.response.delete("1.0", "end")
        self.response.configure(state="disabled")
        for b in self.bubbles:
            b.destroy()
        self.bubbles = []
        self.renderer.clear()

    def bubble_wrap(self):
        """Max text width for a user bubble: ~70% of the chat area (unscaled px)."""
        tb = self.response._textbox
        width = tb.winfo_width() / self.response._get_widget_scaling()
        return max(160, int(width * 0.7) - 28)

    def rewrap_bubbles(self):
        wrap = self.bubble_wrap()
        for b in self.bubbles:
            b.configure(wraplength=wrap)
        self.renderer.resize()

    def add_user_bubble(self, text, index, images=None):
        t = self.t
        tb = self.response._textbox
        thumb = images and thumbnail(images, round(120 * self.response._get_widget_scaling()))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # plain Tk image: we scale it ourselves
            bubble = ctk.CTkLabel(
                tb,
                image=thumb or None,
                compound="top",
                text=text,
                justify="left",
                anchor="w",
                corner_radius=14,
                fg_color=t["bubble"],
                bg_color=t["surface"],
                text_color=t["bubble_text"],
                font=("TkDefaultFont", self.font_size),
                wraplength=self.bubble_wrap(),
                padx=6,
                pady=8,
            )
        bubble.thumb = thumb  # keep a reference so Tk doesn't drop the image
        if thumb:  # click the pictures to open them in the image viewer
            bubble.configure(cursor="hand2")
            bubble.bind("<Button-1>", lambda e: [
                subprocess.Popen(["xdg-open", str(IMAGE_DIR / n)]) for n in images
                if (IMAGE_DIR / n).is_file()])
        # wheel over the bubble should still scroll the chat
        self.renderer.forward_wheel(bubble)
        bubble.bind("<Button-3>", lambda e: self.bubble_menu(e, text, index))
        self.bubbles.append(bubble)

        self.response.configure(state="normal")
        start = tb.index("end-1c")
        tb.window_create("end", window=bubble)
        tb.insert("end", "\n")
        tb.tag_add("user_line", start, "end-1c")
        self.response.see("end")
        self.response.configure(state="disabled")

    def bubble_menu(self, event, text, index):
        def copy():
            self.clipboard_clear()
            self.clipboard_append(text)

        menu = self.themed_menu()
        menu.add_command(label="Copy", command=copy)
        menu.add_command(label="Edit", command=lambda: self.edit_message(index),
                         state="disabled" if self.generating else "normal")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def edit_message(self, i):
        """Cut the chat off at your message i and put its text back in the input box
        to change and send again. The saved chat only changes once you send."""
        if self.generating:
            return
        text = self.history[i]["content"]
        self.pending = list(self.history[i].get("images", []))
        self.history = self.history[:i]
        self.render_history()
        self.refresh_attachments()
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.focus()

    def themed_menu(self):
        t = self.t
        return tk.Menu(
            self,
            tearoff=0,
            bg=t["surface"],
            fg=t["text"],
            activebackground=t["list_hover"],
            activeforeground=t["text"],
            borderwidth=1,
            relief="flat",
        )

    def save_chat(self):
        if not self.history:
            return
        if not self.chat_id:
            self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = CHAT_DIR / f"{self.chat_id}.json"
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = {}
        data = {"title": (self.history[0]["content"].splitlines() or ["Image"])[0][:40]}
        if old.get("renamed"):  # a name you gave it sticks
            data = {"title": old["title"], "renamed": True}
        data["messages"] = self.history
        path.write_text(json.dumps(data, indent=2))
        self.refresh_list()

    def on_search_key(self, event):
        # wait for a pause in typing so each key doesn't re-read every chat
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = self.after(150, self.refresh_list)

    def clear_search(self):
        self.search.delete(0, "end")
        self.refresh_list()
        self.entry.focus()

    def focus_search(self):
        if self.sidebar.winfo_manager() == "":
            self.toggle_sidebar()
        self.search.focus()
        self.search.select_range(0, "end")

    def chat_matches(self, data, words):
        text = " ".join([data["title"]] + [m["content"] for m in data["messages"]]).lower()
        return all(w in text for w in words)

    def refresh_list(self):
        self.search_job = None
        for w in self.chat_list.winfo_children():
            w.destroy()
        words = self.search.get().lower().split()
        chats = []
        for f in sorted(CHAT_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not words or self.chat_matches(data, words):
                chats.append((f, data["title"]))
        if words and not chats:
            ctk.CTkLabel(self.chat_list, text="No chats match", text_color=self.t["muted"]).grid(
                row=0, column=0, sticky="w", padx=10, pady=4)
        self.chat_buttons = []
        for i, (f, title) in enumerate(chats):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # plain Tk image: we scale it ourselves
                btn = ctk.CTkButton(
                    self.chat_list,
                    text=title,
                    anchor="w",
                    fg_color="transparent",
                    hover_color=self.t["list_hover"],
                    text_color=self.t["text"],
                    command=lambda f=f: self.load_chat(f),
                )
                trash = ctk.CTkButton(
                    self.chat_list,
                    text="",
                    image=self._icons["trash-2"],
                    width=28,
                    fg_color="transparent",
                    hover_color=self.t["list_hover"],
                )
            trash.configure(command=lambda f=f, b=trash: self.trash_click(f, b))
            btn.full_title = title
            self.chat_buttons.append(btn)
            btn.bind("<Button-3>", lambda e, f=f, b=btn: self.chat_menu(e, f, b))
            btn.grid(row=i, column=0, sticky="ew", pady=1)
            trash.grid(row=i, column=1, pady=1)
        self.after_idle(self.list_resized)

    def chat_menu(self, event, path, btn):
        menu = self.themed_menu()
        menu.add_command(label="Rename", command=lambda: self.rename_chat(path, btn))
        menu.add_command(label="Export as Markdown…", command=lambda: self.export_chat(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def export_chat(self, path):
        """Save a chat as a Markdown file you pick; its images go in a folder beside it."""
        data = json.loads(path.read_text())
        name = re.sub(r'[\\/:*?"<>|]+', "", data["title"]).strip() or "chat"
        start = Path.home() / "Documents"
        if not start.is_dir():
            start = Path.home()
        self.file_dialog(lambda paths: paths and self.write_markdown(data, Path(paths[0])),
                         True, "Export chat", start / f"{name}.md", "Markdown", ["*.md"])

    def write_markdown(self, data, dest):
        if dest.suffix.lower() != ".md":
            dest = dest.with_name(dest.name + ".md")
        img_dir = dest.with_name(dest.stem + "-images")
        lines = [f"# {data['title']}", ""]
        try:
            for m in data["messages"]:
                lines += ["## You" if m["role"] == "user" else "## Assistant", ""]
                text = strip_think(m["content"]).strip()
                if text:
                    lines += [text, ""]
                for n in m.get("images", []):
                    if (IMAGE_DIR / n).is_file():
                        img_dir.mkdir(exist_ok=True)
                        shutil.copy(IMAGE_DIR / n, img_dir / n)
                        lines += [f"![image](<{img_dir.name}/{n}>)", ""]  # <> allows spaces
                if m.get("stopped"):
                    lines += ["*(stopped)*", ""]
            dest.write_text("\n".join(lines).rstrip() + "\n")
        except OSError as e:
            self.show_note(f"Export failed: {e.strerror or e}", 4000)
            return
        self.show_note(f"Exported to {dest.name}", 3000)

    def rename_chat(self, path, btn):
        """Edit the chat's name in place: Enter saves, Esc or clicking away cancels."""
        t = self.t
        entry = ctk.CTkEntry(
            self.chat_list,
            fg_color=t["window"],
            border_color=t["button"],
            text_color=t["text"],
        )
        entry.insert(0, btn.full_title)
        entry.grid(**{k: v for k, v in btn.grid_info().items() if k != "in"})
        entry.select_range(0, "end")
        entry.focus()

        def save(e):
            name = entry.get().strip()
            if name:
                data = json.loads(path.read_text())
                data["title"], data["renamed"] = name, True
                path.write_text(json.dumps(data, indent=2))
            self.refresh_list()

        entry.bind("<Return>", save)
        entry.bind("<Escape>", lambda e: entry.destroy())
        entry.bind("<FocusOut>", lambda e: entry.winfo_exists() and entry.destroy())

    def list_resized(self):
        self.update_scrollbar()
        self.fit_titles()

    def fit_titles(self):
        """Shorten chat names that don't fit the sidebar with "…" instead of cutting
        them off mid-letter. Redone whenever the list's width changes."""
        pad = round(7 * self.chat_list._get_widget_scaling())  # button's text inset per side
        for btn in self.chat_buttons:
            if not btn.winfo_exists():
                continue
            room = btn.winfo_width() - 2 * pad
            if room <= 0:
                self.after(50, self.fit_titles)  # not laid out yet
                return
            font, text = btn.cget("font"), btn.full_title
            if font.measure(text) > room:
                while text and font.measure(text.rstrip() + "…") > room:
                    text = text[:-1]
                text = text.rstrip() + "…"
            if btn.cget("text") != text:
                btn.configure(text=text)

    def update_scrollbar(self):
        canvas = self.chat_list._parent_canvas
        bbox = canvas.bbox("all")
        if bbox and bbox[3] > canvas.winfo_height():
            self.chat_list._scrollbar.grid()
        else:
            self.chat_list._scrollbar.grid_remove()

    def trash_click(self, path, btn):
        """First click arms the trash can (turns red), a second within 3s deletes."""
        if self.armed == path:
            self.armed = self.armed_btn = None
            self.delete_chat(path)
            return
        self.disarm()
        self.armed, self.armed_btn = path, btn
        btn.configure(image=self._icons["trash-armed"])
        self.after(3000, lambda: self.armed == path and self.disarm())

    def disarm(self):
        btn, self.armed, self.armed_btn = self.armed_btn, None, None
        if btn is not None and btn.winfo_exists():
            btn.configure(image=self._icons["trash-2"])

    def delete_chat(self, path):
        if self.generating:
            return
        path.unlink(missing_ok=True)
        if self.chat_id == path.stem:
            self.new_chat()
        self.refresh_list()
        self.cleanup_images()

    def new_chat(self):
        if self.generating:
            return
        self.history = []
        self.chat_id = None
        self.clear_response()

    def load_chat(self, path):
        if self.generating:
            return
        self.history = json.loads(path.read_text())["messages"]
        self.chat_id = path.stem
        self.render_history()

    def render_history(self):
        self.clear_response()
        for i, m in enumerate(self.history):
            if m["role"] == "user":
                self.add_user_bubble(m["content"], i, m.get("images"))
            else:
                self.renderer.start(think_secs=m.get("think_secs"))
                if m.get("thinking"):
                    self.renderer.think(m["thinking"])
                self.renderer.feed(m["content"])
                self.renderer.finish(stats=m.get("stats"), stopped=m.get("stopped", False),
                                     actions=self.reply_actions(i))


if __name__ == "__main__":
    app = MyApp()
    app.mainloop()