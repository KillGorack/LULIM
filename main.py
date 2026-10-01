#!/usr/bin/env python3
import base64
import io
import json
import math
import queue
import re
import threading
import warnings
import tkinter as tk
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import requests
from PIL import Image, ImageDraw

from render import ReplyRenderer

MODEL = "qwen2.5-coder:7b"
URL = "http://localhost:11434/api/chat"
UNLOAD_URL = "http://localhost:11434/api/generate"
TAGS_URL = "http://localhost:11434/api/tags"
CHAT_DIR = Path.home() / ".local/share/quen/chats"
SETTINGS_FILE = CHAT_DIR.parent / "settings.json"

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


def theme_icon(kind, color, size=18):
    """Draw a sun or moon icon as a Tk image (supersampled for smooth edges).

    Built as PNG data for tk.PhotoImage so it doesn't need PIL.ImageTk
    (a separate distro package on Fedora).
    """
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    if kind == "sun":
        r = s * 0.2
        d.ellipse((c - r, c - r, c + r, c + r), fill=color)
        for i in range(8):
            a = i * math.pi / 4
            x1, y1 = c + math.cos(a) * s * 0.3, c + math.sin(a) * s * 0.3
            x2, y2 = c + math.cos(a) * s * 0.44, c + math.sin(a) * s * 0.44
            d.line((x1, y1, x2, y2), fill=color, width=int(s * 0.07))
    else:
        r = s * 0.38
        d.ellipse((c - r, c - r, c + r, c + r), fill=color)
        o = s * 0.2
        d.ellipse((c - r + o, c - r - o * 0.6, c + r + o, c + r - o * 0.6), fill=(0, 0, 0, 0))
    img = img.resize((size, size), Image.LANCZOS)
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
        self.response.grid(row=0, column=1, sticky="nsew", padx=10, pady=(10, 5))
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
        self.entry.grid(row=1, column=1, sticky="ew", padx=10, pady=(5, 10))
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Shift-Return>", self.on_shift_enter)

        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color="transparent")
        self.sidebar.grid(row=0, column=0, rowspan=2, sticky="ns")
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

        self.new_btn = ctk.CTkButton(
            self.container,
            text="New Chat",
            command=self.new_chat,
        )
        self.new_btn.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=10)

        self.chat_list = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self.chat_list.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=5, pady=(0, 10))
        self.chat_list.grid_columnconfigure(0, weight=1)
        self.chat_list._parent_canvas.bind(
            "<Configure>", lambda e: self.after_idle(self.update_scrollbar), add="+"
        )

        models = self.get_models()
        last = self.settings.get("model", MODEL)
        self.model = last if last in models else (MODEL if MODEL in models else models[0])
        self.model_menu = ctk.CTkOptionMenu(
            self.container,
            values=models,
            command=self.set_model,
        )
        self.model_menu.set(self.model)
        self.title(f"LULIM - {self.model}")
        self.model_menu.grid(row=2, column=0, sticky="ew", padx=(10, 5), pady=(0, 10))

        self.theme_btn = ctk.CTkButton(
            self.container,
            text="",
            width=28,
            command=self.toggle_theme,
        )
        self.theme_btn.grid(row=2, column=1, padx=(0, 10), pady=(0, 10))

        self.history = []
        self.q = queue.Queue()
        self.generating = False
        self.chat_id = None

        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def get_models(self):
        try:
            r = requests.get(TAGS_URL, timeout=3)
            names = [m["name"] for m in r.json()["models"]]
            return names or [MODEL]
        except Exception:
            return [MODEL]

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
        self.chat_list.configure(
            fg_color=t["surface"],
            scrollbar_button_color=t["scrollbar"],
            scrollbar_button_hover_color=t["scrollbar_hover"],
        )
        self.new_btn.configure(
            fg_color=t["button"], hover_color=t["button_hover"], text_color=t["text"]
        )
        self.model_menu.configure(
            fg_color=t["button"],
            button_color=t["menu_button"],
            button_hover_color=t["button_hover"],
            text_color=t["text"],
            dropdown_fg_color=t["surface"],
            dropdown_hover_color=t["list_hover"],
            dropdown_text_color=t["text"],
        )
        # show the theme you'd switch to: sun while dark, moon while light
        icon = "sun" if self.theme_name == "dark" else "moon"
        size = round(18 * self.theme_btn._get_widget_scaling())
        self._theme_img = theme_icon(icon, t["text"], size)  # keep a reference
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # plain Tk image: we scale it ourselves
            self.theme_btn.configure(
                image=self._theme_img,
                fg_color=t["button"],
                hover_color=t["button_hover"],
            )
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
        self.add_user_bubble(text)
        self.renderer.start()
        self.stats = None
        threading.Thread(target=self.worker, daemon=True).start()
        self.after(50, self.poll)

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
            with requests.post(
                URL,
                json={"model": self.model, "messages": self.payload_messages(), "stream": True},
                stream=True,
                timeout=300,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
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
                        break
        except Exception as e:
            self.q.put(("error", f"[error: {e}]"))
        if reply:
            entry = {"role": "assistant", "content": reply}
            if thinking:
                entry["thinking"] = thinking
            if stats:
                entry["stats"] = stats
            self.history.append(entry)
        self.q.put(("done", stats))

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
                    self.renderer.finish(stats=data)
                    secs = self.renderer.think_secs
                    if secs is not None and self.history and self.history[-1]["role"] == "assistant":
                        self.history[-1]["think_secs"] = secs
                    self.generating = False
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
            btn = ctk.CTkButton(
                self.chat_list,
                text=title,
                anchor="w",
                fg_color="transparent",
                hover_color=self.t["list_hover"],
                text_color=self.t["text"],
                command=lambda f=f: self.load_chat(f),
            )
            btn.grid(row=i, column=0, sticky="ew", pady=1)
            btn.bind("<Button-3>", lambda e, f=f: self.chat_menu(e, f))
        self.after_idle(self.update_scrollbar)

    def update_scrollbar(self):
        canvas = self.chat_list._parent_canvas
        bbox = canvas.bbox("all")
        if bbox and bbox[3] > canvas.winfo_height():
            self.chat_list._scrollbar.grid()
        else:
            self.chat_list._scrollbar.grid_remove()

    def chat_menu(self, event, path):
        menu = self.themed_menu()
        menu.add_command(label="Delete", command=lambda: self.delete_chat(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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


if __name__ == "__main__":
    app = MyApp()
    app.mainloop()