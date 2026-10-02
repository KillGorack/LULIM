#!/usr/bin/env python3
import base64
import io
import json
import queue
import re
import threading
import warnings
import tkinter as tk
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
ICON_DIR = Path(__file__).with_name("icons")  # Lucide icons (ISC), see icons/LICENSE

THEMES = {
    "dark": {
        "window": "#262626",
        "surface": "#171717",
        "text": "#f5f5f5",
        "bubble": "#2e2e2e",
        "bubble_text": "#f5f5f5",
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
        "menu_button": "#404040",
        "list_hover": "#333333",
        "scrollbar": "#404040",
        "scrollbar_hover": "#525252",
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
        "menu_button": "#665c4c",
        "list_hover": "#cbc2ae",
        "scrollbar": "#a89e8a",
        "scrollbar_hover": "#8c806b",
    },
}

FONT_SIZES = {"user": 13, "ai": 13, "entry": 13}


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
            font=("TkDefaultFont", FONT_SIZES["ai"]),
        )
        self.response.grid(row=0, column=1, sticky="nsew", padx=10, pady=(10, 0))
        tb = self.response._textbox
        tb.configure(padx=8, pady=6)
        # user bubbles sit on their own right-justified line
        tb.tag_config("user_line", justify="right", spacing1=14, spacing3=12, rmargin=2)
        self.renderer = ReplyRenderer(self.response, FONT_SIZES["ai"])
        tb.bind("<Configure>", lambda e: self.rewrap_bubbles(), add="+")
        self.bubbles = []

        self.entry = ctk.CTkTextbox(
            self,
            height=70,
            wrap="word",
            font=("TkDefaultFont", FONT_SIZES["entry"]),
        )
        self.entry.grid(row=2, column=1, sticky="ew", padx=10, pady=(0, 10))
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Shift-Return>", self.on_shift_enter)
        self.bind("<Escape>", lambda e: self.stop())

        # only shown while a reply is streaming, over the right edge of the entry
        self.stop_btn = ctk.CTkButton(self.entry, text="Stop", width=60, command=self.stop)

        # button bar between chat and entry: sidebar toggle and new chat on the left,
        # model and theme on the right
        self.toolbar = ctk.CTkFrame(self, corner_radius=self.response.cget("corner_radius"))
        self.toolbar.grid(row=1, column=1, sticky="ew", padx=10, pady=5)
        self.toolbar.grid_columnconfigure(2, weight=1)

        self.sidebar_btn = ctk.CTkButton(
            self.toolbar, text="", width=32, command=self.toggle_sidebar
        )
        self.sidebar_btn.grid(row=0, column=0, padx=(6, 2), pady=6)
        self.new_btn = ctk.CTkButton(self.toolbar, text="", width=32, command=self.new_chat)
        self.new_btn.grid(row=0, column=1, pady=6)

        models = self.get_models()
        last = self.settings.get("model", MODEL)
        self.model = last if last in models else (MODEL if MODEL in models else models[0])
        self.model_menu = ctk.CTkOptionMenu(
            self.toolbar,
            values=models,
            width=200,
            command=self.set_model,
        )
        self.model_menu.set(self.model)
        self.title(f"LULIM - {self.model}")
        self.model_menu.grid(row=0, column=3, padx=(0, 4), pady=6)

        self.theme_btn = ctk.CTkButton(
            self.toolbar,
            text="",
            width=32,
            command=self.toggle_theme,
        )
        self.theme_btn.grid(row=0, column=4, padx=(0, 6), pady=6)

        # sidebar: just the saved chats
        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color="transparent")
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="ns")
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

        self.list_title = ctk.CTkLabel(
            self.container,
            text="Previous chats",
            anchor="w",
            font=("TkDefaultFont", FONT_SIZES["ai"], "bold"),
        )
        self.list_title.grid(row=0, column=0, sticky="ew", padx=15, pady=(10, 0))

        self.chat_list = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self.chat_list.grid(row=1, column=0, sticky="nsew", padx=5, pady=(4, 10))
        self.chat_list.grid_columnconfigure(0, weight=1)
        self.chat_list._parent_canvas.bind(
            "<Configure>", lambda e: self.after_idle(self.update_scrollbar), add="+"
        )
        self.bind("<Control-b>", lambda e: self.toggle_sidebar())

        self.history = []
        self.q = queue.Queue()
        self.generating = False
        self.model_ctx = {}  # model -> trained context length (None if unknown)
        self.stop_event = threading.Event()
        self.resp = None
        self.chat_id = None
        self.armed = self.armed_btn = None  # chat whose trash can was clicked once

        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        self.apply_theme()
        if self.settings.get("sidebar_hidden"):
            self.sidebar.grid_remove()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def get_models(self):
        try:
            r = requests.get(TAGS_URL, timeout=3)
            names = [m["name"] for m in r.json()["models"]]
            return names or [MODEL]
        except Exception:
            return [MODEL]

    def num_ctx(self):
        """Context window for the current model: the setting, capped at the model's max."""
        want = self.settings.get("num_ctx", NUM_CTX)
        if self.model not in self.model_ctx:
            limit = None
            try:
                r = requests.post(SHOW_URL, json={"model": self.model}, timeout=5)
                info = r.json().get("model_info", {})
                limit = next((v for k, v in info.items() if k.endswith(".context_length")), None)
            except Exception:
                pass
            self.model_ctx[self.model] = limit
        limit = self.model_ctx[self.model]
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

    def toggle_sidebar(self):
        hidden = self.sidebar.winfo_manager() == ""
        if hidden:
            self.sidebar.grid()
        else:
            self.sidebar.grid_remove()
        self.save_setting("sidebar_hidden", not hidden)

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
        self.list_title.configure(text_color=t["text"])
        self.chat_list.configure(
            fg_color=t["surface"],
            scrollbar_button_color=t["scrollbar"],
            scrollbar_button_hover_color=t["scrollbar_hover"],
        )
        self.toolbar.configure(fg_color=t["surface"])
        # flat icon buttons on the bar: just the icon, highlighted on hover
        for btn in (self.sidebar_btn, self.new_btn, self.theme_btn):
            btn.configure(fg_color="transparent", hover_color=t["list_hover"])
        self.stop_btn.configure(
            fg_color=t["button"], hover_color=t["button_hover"], text_color=t["text"]
        )
        self.stop_btn.configure(bg_color=t["surface"])  # rounded corners sit on the entry
        self.model_menu.configure(
            fg_color=t["button"],
            button_color=t["menu_button"],
            button_hover_color=t["button_hover"],
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
                              (self.theme_btn, theme)):
                self._icons[name] = load_icon(name, t["text"], size)
                btn.configure(image=self._icons[name])
        list_size = round(16 * self.theme_btn._get_widget_scaling())
        self._icons["message-square"] = load_icon("message-square", t["muted"], list_size)
        self._icons["trash-2"] = load_icon("trash-2", t["muted"], list_size)
        self._icons["trash-armed"] = load_icon("trash-2", t["error"], list_size)
        self.refresh_list()

    def close(self):
        if self.generating:
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
        if not text or self.generating:
            return
        self.entry.delete("1.0", "end")
        self.history.append({"role": "user", "content": text})
        self.generating = True
        self.stop_event.clear()
        self.stop_btn.place(relx=1, rely=0.5, anchor="e", x=-22)
        self.add_user_bubble(text)
        self.renderer.start()
        self.stats = None
        threading.Thread(target=self.worker, daemon=True).start()
        self.after(50, self.poll)

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
        """History as sent to the model: no saved thinking, stats or <think> blocks."""
        return [
            {"role": m["role"],
             "content": re.sub(r"<think>.*?</think>\s*", "", m["content"], flags=re.S)}
            for m in self.history
        ]

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
                elif kind == "done":
                    stats, stopped = data
                    self.renderer.finish(stats=stats)
                    if stopped:
                        self.renderer.mark_stopped()
                    secs = self.renderer.think_secs
                    if secs is not None and self.history and self.history[-1]["role"] == "assistant":
                        self.history[-1]["think_secs"] = secs
                    self.generating = False
                    self.stop_btn.place_forget()
                    self.save_chat()
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

    def add_user_bubble(self, text):
        t = self.t
        tb = self.response._textbox
        bubble = ctk.CTkLabel(
            tb,
            text=text,
            justify="left",
            anchor="w",
            corner_radius=14,
            fg_color=t["bubble"],
            bg_color=t["surface"],
            text_color=t["bubble_text"],
            font=("TkDefaultFont", FONT_SIZES["user"]),
            wraplength=self.bubble_wrap(),
            padx=6,
            pady=8,
        )
        # wheel over the bubble should still scroll the chat
        self.renderer.forward_wheel(bubble)
        bubble.bind("<Button-3>", lambda e: self.bubble_menu(e, text))
        self.bubbles.append(bubble)

        self.response.configure(state="normal")
        start = tb.index("end-1c")
        tb.window_create("end", window=bubble)
        tb.insert("end", "\n")
        tb.tag_add("user_line", start, "end-1c")
        self.response.see("end")
        self.response.configure(state="disabled")

    def bubble_menu(self, event, text):
        def copy():
            self.clipboard_clear()
            self.clipboard_append(text)

        menu = self.themed_menu()
        menu.add_command(label="Copy", command=copy)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
        title = self.history[0]["content"].splitlines()[0][:40]
        data = {"title": title, "messages": self.history}
        (CHAT_DIR / f"{self.chat_id}.json").write_text(json.dumps(data, indent=2))
        self.refresh_list()

    def refresh_list(self):
        for w in self.chat_list.winfo_children():
            w.destroy()
        for i, f in enumerate(sorted(CHAT_DIR.glob("*.json"), reverse=True)):
            title = json.loads(f.read_text())["title"]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # plain Tk image: we scale it ourselves
                btn = ctk.CTkButton(
                    self.chat_list,
                    text=title,
                    image=self._icons["message-square"],
                    compound="left",
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
            btn.grid(row=i, column=0, sticky="ew", pady=1)
            trash.grid(row=i, column=1, pady=1)
        self.after_idle(self.update_scrollbar)

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
        self.clear_response()
        for m in self.history:
            if m["role"] == "user":
                self.add_user_bubble(m["content"])
            else:
                self.renderer.start(think_secs=m.get("think_secs"))
                if m.get("thinking"):
                    self.renderer.think(m["thinking"])
                self.renderer.feed(m["content"])
                self.renderer.finish(stats=m.get("stats"))
                if m.get("stopped"):
                    self.renderer.mark_stopped()


if __name__ == "__main__":
    app = MyApp()
    app.mainloop()