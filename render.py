"""Streaming Markdown renderer for assistant replies in a Tk text widget.

Models mark up their replies with Markdown (``` fences, **bold**, lists, ...)
and thinking models send their reasoning separately or inside <think> tags.
Tokens arrive in arbitrary pieces, so text is only held back while it is
still ambiguous (e.g. a line starting with "-" could be a list or a rule).
"""
import re
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser

try:  # optional: code blocks stay plain without it
    from pygments.lexers import get_lexer_by_name
    from pygments.token import Comment, Keyword, Name, Number, String
    from pygments.util import ClassNotFound
except ImportError:
    get_lexer_by_name = None

BULLETS = ["•", "◦", "▪", "▪"]
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
# Pygments token type -> tag, first match wins; colors come from the theme
SYNTAX = [] if get_lexer_by_name is None else [
    (Comment.Preproc, "syn_keyword"),
    (Comment, "syn_comment"),
    (String, "syn_string"),
    (Number, "syn_number"),
    (Keyword, "syn_keyword"),
    (Name.Tag, "syn_keyword"),
    (Name.Builtin, "syn_builtin"),
    (Name.Function, "syn_func"),
    (Name.Class, "syn_func"),
    (Name.Decorator, "syn_func"),
    (Name.Attribute, "syn_func"),
]
SYNTAX_TAGS = ("syn_keyword", "syn_comment", "syn_string", "syn_number", "syn_builtin",
               "syn_func")


class ReplyRenderer:
    def __init__(self, textbox, size):
        self.box = textbox  # the CTkTextbox (for scaling)
        self.tb = tb = textbox._textbox
        self.scale = textbox._get_widget_scaling()
        self.t = None
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        # code header labels: fixed size, kept for the widget's lifetime (dropping a
        # tkfont.Font deletes it from Tk, which would reset labels still using it)
        self.head_font = tkfont.Font(family=fam, size=-round(11 * self.scale))
        self.set_size(size)
        # priority (low -> high): inline styles beat line styles, headings beat bold
        for tag in ("ai", "error", "quote", "li0", "li1", "li2", "li3", "bold", "italic",
                    "bolditalic", "strike", "h1", "h2", "h3", "table", "inline", "inline_gap",
                    "link", "code", "think", "think_head", "stats"):
            tb.tag_raise(tag)
        for tag in SYNTAX_TAGS:  # above "code" so their colors win
            tb.tag_config(tag)
            tb.tag_raise(tag)
        for tag in ("link", "think_head", "action"):
            tb.tag_bind(tag, "<Enter>", lambda e: tb.configure(cursor="hand2"))
            tb.tag_bind(tag, "<Leave>", lambda e: tb.configure(cursor="xterm"))

        self.code_heads, self.rules, self.code_blocks = [], [], {}
        self.action_labels = {}  # action link -> its label while it shows e.g. "Copied"
        self.counter = 0
        self.start()

    # ------------------------------------------------------------ theme / layout

    def set_size(self, size):
        """(Re)build the fonts for text size `size`; used again when zooming."""
        tb, scale = self.tb, self.scale
        px = lambda n: -round(n * scale)  # negative = pixels, matching CTk
        sp = lambda n: round(n * scale)
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        code_font = (mono, px(size))
        small = (fam, px(size - 2))

        tb.tag_config("ai", font=(fam, px(size)), lmargin1=4, lmargin2=4, rmargin=4,
                      spacing2=2, spacing3=2)
        tb.tag_config("error", font=(fam, px(size)), lmargin1=4, lmargin2=4)
        tb.tag_config("quote", lmargin1=sp(18), lmargin2=sp(18))
        for lvl in range(4):
            base = 4 + lvl * sp(18)
            hang = base + sp(18)
            tb.tag_config(f"li{lvl}", lmargin1=base, lmargin2=hang, tabs=(hang,))
        tb.tag_config("bold", font=(fam, px(size), "bold"))
        tb.tag_config("italic", font=(fam, px(size), "italic"))
        tb.tag_config("bolditalic", font=(fam, px(size), "bold", "italic"))
        tb.tag_config("strike", overstrike=1)
        tb.tag_config("h1", font=(fam, px(size + 6), "bold"), spacing1=sp(10), spacing3=sp(4))
        tb.tag_config("h2", font=(fam, px(size + 3), "bold"), spacing1=sp(8), spacing3=sp(3))
        tb.tag_config("h3", font=(fam, px(size + 1), "bold"), spacing1=sp(6), spacing3=sp(2))
        tb.tag_config("table", font=code_font)
        tb.tag_config("inline", font=code_font)
        # spaces inside inline code get no background: Tk stretches a
        # highlighted space at a line wrap all the way to the right edge
        tb.tag_config("inline_gap", font=code_font)
        tb.tag_config("link", underline=1)
        # code blocks: margins painted in the code color so the block is one panel
        tb.tag_config("code", font=code_font, lmargin1=14, lmargin2=14, rmargin=14)
        tb.tag_config("code_top", spacing1=sp(10))  # gap above the header bar
        tb.tag_config("code_pad", font=(fam, px(6)))
        tb.tag_config("rule_line", spacing1=sp(8), spacing3=sp(8))
        tb.tag_config("think_head", font=small, spacing1=sp(2), spacing3=sp(6), lmargin1=4)
        tb.tag_config("think", font=small, lmargin1=sp(16), lmargin2=sp(16), spacing3=sp(2))
        tb.tag_config("stats", font=small, lmargin1=4, spacing1=sp(6))


    def apply_theme(self, t):
        self.t = t
        tb = self.tb
        tb.tag_config("ai", foreground=t["ai_text"])
        tb.tag_config("error", foreground=t["error"])
        for tag in ("quote", "think", "think_head", "stats"):
            tb.tag_config(tag, foreground=t["muted"])
        for tag in ("code", "code_pad"):
            tb.tag_config(tag, background=t["code_bg"], lmargincolor=t["code_bg"],
                          rmargincolor=t["code_bg"])
        tb.tag_config("code", foreground=t["text"])
        for tag in SYNTAX_TAGS:
            tb.tag_config(tag, foreground=t[tag])
        tb.tag_config("inline", background=t["code_bg"], foreground=t["text"])
        tb.tag_config("link", foreground=t["link"])
        for bar, *labels in self.code_heads:
            bar.configure(bg=t["code_head_bg"])
            for lbl in labels:
                lbl.configure(bg=t["code_head_bg"], fg=t["code_head"])
        for rule in self.rules:
            rule.configure(bg=t["rule"])

    def full_width(self):
        return max(100, self.tb.winfo_width() - 2 * int(self.tb.cget("padx")) - 2)

    def resize(self):
        width = self.full_width()
        for bar, *_ in self.code_heads:
            bar.configure(width=width)
        for rule in self.rules:
            rule.configure(width=width)

    def clear(self):
        for bar, *_ in self.code_heads:
            bar.destroy()
        for rule in self.rules:
            rule.destroy()
        self.code_heads, self.rules, self.code_blocks = [], [], {}

    def forward_wheel(self, widget):
        """Embedded widgets swallow wheel events; pass them on to the chat."""
        tb = self.tb
        widget.bind("<MouseWheel>", lambda e: tb.event_generate("<MouseWheel>", delta=e.delta), add="+")
        widget.bind("<Button-4>", lambda e: tb.yview_scroll(-3, "units"), add="+")
        widget.bind("<Button-5>", lambda e: tb.yview_scroll(3, "units"), add="+")

    # ------------------------------------------------------------ low-level output

    def insert(self, text, tags=()):
        if not text:
            return
        follow = self.at_bottom()
        self.tb.configure(state="normal")
        self.tb.insert("end", text, tags)
        if follow:
            self.tb.see("end")
        self.tb.configure(state="disabled")

    def embed(self, widget, line_tag):
        tb = self.tb
        follow = self.at_bottom()
        tb.configure(state="normal")
        if tb.get("end-2c", "end-1c") not in ("", "\n"):
            tb.insert("end", "\n")
        elif tb.get("end-3c", "end-1c") == "\n\n":
            tb.delete("end-2c")  # spacing on the embed line replaces the blank line
        start = tb.index("end-1c")
        tb.window_create("end", window=widget)
        tb.insert("end", "\n")
        tb.tag_add(line_tag, start, "end-1c")
        if follow:
            tb.see("end")
        tb.configure(state="disabled")

    def at_bottom(self):
        """True if the end of the chat is on screen: only then does new output scroll it.
        Scrolling up while a reply streams leaves the view where it is."""
        return self.tb.bbox("end-1c") is not None

    def new_id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    # ------------------------------------------------------------ public stream API

    def start(self, think_secs=None):
        """Reset state for a new assistant reply."""
        self.raw = ""               # content not yet routed (think-tag detection)
        self.think_state = "start"  # start | in | out  (for <think> tags in content)
        self.think_open = None      # (head_tag, body_tag) while thinking streams
        self.think_t0 = None
        self.think_secs = None
        self.saved_think_secs = think_secs
        self.pending = ""           # markdown text not yet emitted
        self.at_line_start = True
        self.reply_started = False
        self.in_code = False
        self.line_tags = ("ai",)
        self.skip_blank = False     # swallow the blank line after a divider
        self.hold = ""              # inline text held back (e.g. trailing "*")
        self.bold = self.italic = self.strike = self.code = False
        self.prev = " "

    def think(self, text):
        """Reasoning text sent separately by the model (Ollama's 'thinking' field)."""
        if not self.think_open:
            text = text.lstrip()
            if not text:
                return
            self.open_think()
        self.insert(text, ("think", self.think_open[1]))

    def feed(self, text):
        """Reply content; may contain <think>...</think> from older setups."""
        self.raw += text
        while self.raw:
            if self.think_state == "start":
                s = self.raw.lstrip()
                if not s or (len(s) < len(THINK_OPEN) and THINK_OPEN.startswith(s)):
                    return
                if s.startswith(THINK_OPEN):
                    self.raw = s[len(THINK_OPEN):]
                    self.think_state = "in"
                    continue
                self.think_state = "out"
            if self.think_state == "in":
                i = self.raw.find(THINK_CLOSE)
                if i >= 0:
                    self.think(self.raw[:i])
                    self.raw = self.raw[i + len(THINK_CLOSE):]
                    self.end_think()
                    self.think_state = "out"
                    continue
                keep = next((k for k in range(min(len(THINK_CLOSE) - 1, len(self.raw)), 0, -1)
                             if THINK_CLOSE.startswith(self.raw[-k:])), 0)
                self.think(self.raw[: len(self.raw) - keep])
                self.raw = self.raw[len(self.raw) - keep:]
                return
            text, self.raw = self.raw, ""
            self.markdown(text)

    def finish(self, stats=None, stopped=False, actions=()):
        """End the reply and add its footer line: stats, "stopped", then action links
        given as (label, callback, label shown briefly after a click or None)."""
        if self.think_state == "in":
            self.think(self.raw)
            self.raw = ""
        elif self.raw:
            self.think_state = "out"
            text, self.raw = self.raw, ""
            self.markdown(text)
        self.end_think()
        if self.pending or self.hold:
            self.pending += "\n"
            self.run_lines()
        if self.in_code:
            self.fence("")  # model never closed the block
        if self.tb.get("end-2c", "end-1c") not in ("", "\n"):
            self.insert("\n")
        parts = []
        if stats and stats.get("tokens"):
            parts.append(f"{stats['tps']:.1f} tok/s  ·  {stats['tokens']} tokens")
            if stats.get("ctx_max"):
                pct = round(100 * stats["ctx"] / stats["ctx_max"])
                parts.append(f"context {stats['ctx']:,} / {stats['ctx_max']:,} ({pct}%)")
        if stopped:
            parts.append("stopped")
        line = "  ·  ".join(parts)
        if not line and not actions:
            return
        self.insert(line, ("stats",))
        for i, (label, callback, flash) in enumerate(actions):
            group = f"act_{label}"  # separator + link, so drop_actions() can remove both
            sep = ("      " if i == 0 else "  ·  ") if line or i else ""
            self.insert(sep, ("stats", group))
            link = self.new_id("action")
            self.insert(label, ("stats", "action", group, link))
            self.tb.tag_bind(link, "<Button-1>",
                             lambda e, c=callback, l=link, f=flash: self.run_action(c, l, f))
        self.insert("\n", ("stats",))

    def run_action(self, callback, link, flash):
        if flash:
            self.relabel(link, flash)
            self.tb.after(1500, lambda: self.relabel(link, None))
        callback()

    def relabel(self, link, text):
        """Swap an action link's text (e.g. Copy -> Copied); None restores the original."""
        tb = self.tb
        r = tb.tag_ranges(link)
        if not r:
            return  # chat was cleared meanwhile
        old = tb.get(r[0], r[1])
        if text is None:
            text = self.action_labels.pop(link, old)
        else:
            self.action_labels.setdefault(link, old)
        tags = tb.tag_names(r[0])
        tb.configure(state="normal")
        tb.delete(r[0], r[1])
        tb.insert(r[0], text, tags)
        tb.configure(state="disabled")

    def drop_actions(self, *labels):
        """Remove action links (with their separators), e.g. Regenerate once it no
        longer applies to the latest reply."""
        tb = self.tb
        tb.configure(state="normal")
        for label in labels:
            r = tb.tag_ranges(f"act_{label}")
            for k in range(len(r) - 2, -1, -2):  # back to front keeps indices valid
                tb.delete(r[k], r[k + 1])
        tb.configure(state="disabled")

    def show_error(self, msg):
        self.finish()
        self.insert(msg.strip() + "\n", ("error",))

    # ------------------------------------------------------------ thinking section

    def open_think(self):
        head, body = self.new_id("thead"), self.new_id("tbody")
        self.think_open = (head, body)
        self.think_t0 = time.monotonic()
        self.insert("▾ Thinking…\n", ("think_head", head))
        self.tb.tag_bind(head, "<Button-1>", lambda e: self.toggle_think(head, body))

    def end_think(self):
        if not self.think_open:
            return
        head, body = self.think_open
        self.think_open = None
        tb = self.tb
        r = tb.tag_ranges(body)
        if not r or not tb.get(r[0], r[-1]).strip():  # nothing worth showing
            hr = tb.tag_ranges(head)
            tb.configure(state="normal")
            tb.delete(hr[0], r[-1] if r else hr[-1])
            tb.configure(state="disabled")
            return
        if tb.get("end-2c", "end-1c") != "\n":
            self.insert("\n", ("think", body))
        if self.saved_think_secs is not None:
            secs = self.saved_think_secs
        else:
            secs = time.monotonic() - self.think_t0
        self.think_secs = round(secs, 1)
        label = f"Thought for {secs:.0f}s" if secs >= 1 else "Thought for a moment"
        self.set_think_head(head, f"▸ {label}")
        tb.tag_config(body, elide=True)

    def set_think_head(self, head, text):
        tb = self.tb
        r = tb.tag_ranges(head)
        tb.configure(state="normal")
        tb.delete(r[0], r[1])
        tb.insert(r[0], text + "\n", ("think_head", head))
        tb.configure(state="disabled")

    def toggle_think(self, head, body):
        if self.think_open and self.think_open[0] == head:
            return  # still streaming
        hidden = self.tb.tag_cget(body, "elide") in ("1", 1, True)
        self.tb.tag_config(body, elide=not hidden)
        r = self.tb.tag_ranges(head)
        text = self.tb.get(r[0], r[1]).rstrip("\n")
        self.set_think_head(head, ("▾" if hidden else "▸") + text[1:])

    # ------------------------------------------------------------ block level

    def markdown(self, text):
        if not self.reply_started:
            text = text.lstrip()  # some models start replies with a space
            if not text:
                return
            self.reply_started = True
            self.end_think()
        self.pending += text
        self.run_lines()

    def run_lines(self):
        while self.pending:
            if self.at_line_start:
                if self.in_code:
                    head = self.pending.lstrip(" ")
                    if head.startswith("```") or ("```".startswith(head) and "\n" not in head):
                        if "\n" not in self.pending:
                            return
                        if head.startswith("```"):
                            _, self.pending = self.pending.split("\n", 1)
                            self.fence("")
                            continue
                    self.at_line_start = False
                else:
                    if self.skip_blank:
                        self.skip_blank = False
                        if self.pending.startswith("\n"):
                            self.pending = self.pending[1:]
                            continue
                    kind = self.classify(self.pending)
                    if kind is None:
                        return  # still ambiguous, wait for more text
                    name, used, info = kind
                    if name in ("fence", "rule"):
                        _, self.pending = self.pending.split("\n", 1)
                        self.fence(info) if name == "fence" else self.add_rule()
                        continue
                    self.pending = self.pending[used:]
                    self.start_line(name, info)
                    self.at_line_start = False
            i = self.pending.find("\n")
            if i == -1:
                chunk, self.pending = self.pending, ""
            else:
                chunk, self.pending = self.pending[: i + 1], self.pending[i + 1:]
            if self.in_code:
                self.code_blocks[self.block_id] += chunk
                self.insert(chunk, ("code",))
                if i != -1:
                    self.highlight()
            else:
                self.inline(chunk, line_end=i != -1)
            if i != -1:
                self.at_line_start = True
                self.line_tags = ("ai",)
                self.bold = self.italic = self.strike = self.code = False
                self.prev = " "

    @staticmethod
    def maybe_marker(line):
        return (line == "" or "```".startswith(line)
                or re.fullmatch(r"#{1,6}|[-*_+]+|\d{1,3}[.)]?|>", line) is not None)

    def classify(self, p):
        h = p.lstrip(" ")
        indent = len(p) - len(h)
        nl = "\n" in h
        line = h.split("\n", 1)[0]
        if not nl and (self.maybe_marker(line) or line.startswith("```")):
            return None
        if line.startswith("```"):
            return ("fence", 0, line[3:].strip())
        if re.fullmatch(r"([-*_])(\s*\1){2,}\s*", line):
            return ("rule", 0, None)
        m = re.match(r"(#{1,6})[ \t]+", line)
        if m:
            return ("heading", indent + m.end(), len(m.group(1)))
        m = re.match(r"[-*+][ \t]+", line)
        if m:
            return ("bullet", indent + m.end(), (BULLETS[min(indent // 2, 3)], indent))
        m = re.match(r"(\d{1,3}[.)])[ \t]+", line)
        if m:
            return ("bullet", indent + m.end(), (m.group(1), indent))
        if line.startswith(">"):
            return ("quote", indent + (2 if line[1:2] == " " else 1), None)
        if line.startswith("|"):
            return ("table", indent, None)
        return ("plain", 0, None)

    def start_line(self, name, info):
        if name == "heading":
            self.line_tags = ("ai", f"h{min(info, 3)}")
        elif name == "bullet":
            marker, indent = info
            self.line_tags = ("ai", f"li{min(indent // 2, 3)}")
            self.insert(marker + "\t", self.line_tags)
        elif name == "quote":
            self.line_tags = ("ai", "quote", "italic")
        elif name == "table":
            self.line_tags = ("ai", "table")
        else:
            self.line_tags = ("ai",)

    def fence(self, lang):
        if not self.in_code:
            self.in_code = True
            self.block_id = len(self.code_blocks)
            self.code_blocks[self.block_id] = ""
            self.add_code_header(lang or "code", self.block_id)
            self.insert("\n", ("code_pad",))
            self.lexer = self.get_lexer(lang)
            # left gravity: stays put while the block's text is added after it
            self.tb.mark_set("code_start", "end-1c")
            self.tb.mark_gravity("code_start", "left")
        else:
            self.in_code = False
            self.highlight()  # a last line without a newline
            if not self.code_blocks[self.block_id].endswith("\n"):
                self.insert("\n", ("code",))
            self.insert("\n", ("code_pad",))

    @staticmethod
    def get_lexer(lang):
        """Pygments lexer for the fence's language, or None: no name, unknown name
        or no Pygments all mean plain code (guessing the language is too often wrong)."""
        if get_lexer_by_name is None or not lang:
            return None
        try:
            # keep the text exactly as inserted so token offsets match the widget
            return get_lexer_by_name(lang.split()[0], stripnl=False, ensurenl=False)
        except ClassNotFound:
            return None

    def highlight(self):
        """Re-color the code block streamed so far. The whole block is lexed again each
        time so multi-line strings and comments come out right; blocks are small."""
        if not self.lexer:
            return
        tb = self.tb
        for tag in SYNTAX_TAGS:
            tb.tag_remove(tag, "code_start", "end")
        pos = 0
        for ttype, value in self.lexer.get_tokens(self.code_blocks[self.block_id]):
            tag = next((tag for base, tag in SYNTAX if ttype in base), None)
            if tag and value.strip():
                tb.tag_add(tag, f"code_start+{pos}c", f"code_start+{pos + len(value)}c")
            pos += len(value)

    def add_code_header(self, lang, block):
        t = self.t
        pad = round(12 * self.scale)
        bar = tk.Frame(self.tb, bg=t["code_head_bg"], width=self.full_width(),
                       height=self.head_font.metrics("linespace") + pad)
        bar.pack_propagate(False)
        style = dict(bg=t["code_head_bg"], fg=t["code_head"], font=self.head_font, bd=0)
        lang_lbl = tk.Label(bar, text=lang, **style)
        lang_lbl.pack(side="left", padx=(14, 0))
        copy_lbl = tk.Label(bar, text="Copy", cursor="hand2", **style)
        copy_lbl.pack(side="right", padx=(0, 14))
        copy_lbl.bind("<Button-1>", lambda e: self.copy_code(block, copy_lbl))
        for w in (bar, lang_lbl, copy_lbl):
            self.forward_wheel(w)
        self.code_heads.append((bar, lang_lbl, copy_lbl))
        self.embed(bar, "code_top")

    def copy_code(self, block, label):
        self.tb.clipboard_clear()
        self.tb.clipboard_append(self.code_blocks.get(block, "").rstrip("\n"))
        label.configure(text="Copied")
        self.tb.after(1500, lambda: label.winfo_exists() and label.configure(text="Copy"))

    def add_rule(self):
        rule = tk.Frame(self.tb, bg=self.t["rule"], width=self.full_width(),
                        height=max(1, round(self.scale)))
        self.forward_wheel(rule)
        self.rules.append(rule)
        self.embed(rule, "rule_line")
        self.skip_blank = True

    # ------------------------------------------------------------ inline level

    def style_tags(self):
        if self.code:
            return ("inline",)
        tags = ()
        if self.bold and self.italic:
            tags = ("bolditalic",)
        elif self.bold:
            tags = ("bold",)
        elif self.italic:
            tags = ("italic",)
        return tags + (("strike",) if self.strike else ())

    def out(self, text):
        if not text:
            return
        if self.code:
            heading = self.line_tags[-1] in ("h1", "h2", "h3")
            for piece in re.split(r"(\s+)", text):
                if piece:
                    plain = heading or piece.isspace()
                    self.insert(piece, self.line_tags + (("inline_gap",) if plain else ("inline",)))
        elif text == "\n":
            self.insert(text, self.line_tags)
        else:
            self.insert(text, self.line_tags + self.style_tags())
        self.prev = text[-1]

    def inline(self, chunk, line_end):
        s, self.hold = self.hold + chunk, ""
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if self.code:
                j = s.find("`", i)
                if j == -1:
                    self.out(s[i:])
                    return
                self.out(s[i:j])
                self.code = False
                i = j + 1
                continue
            if c == "`":
                self.code = True
                i += 1
                continue
            if c in "*~":
                run = len(s[i:]) - len(s[i:].lstrip(c))
                if i + run == n and not line_end:
                    self.hold = s[i:]  # need the next character to decide
                    return
                used = self.emphasis(c, run, s[i + run] if i + run < n else "\n")
                if not used:
                    self.out(s[i:i + run])
                    used = run
                i += used
                continue
            if c == "[":
                link = self.link(s, i, line_end)
                if link == "wait":
                    self.hold = s[i:]
                    return
                if link:
                    i = link
                    continue
                self.out("[")
                i += 1
                continue
            j = i
            while j < n and s[j] not in "`*~[":
                j += 1
            if s[j - 1:j] == "\n" and j - 1 > i:
                self.out(s[i:j - 1])
                self.out("\n")
            else:
                self.out(s[i:j])
            i = j

    def emphasis(self, c, run, nxt):
        """Toggle bold/italic/strike if the marker is in a valid spot; return chars used."""
        prv = self.prev
        if c == "~":
            if run < 2:
                return 0
            closing = self.strike
            if (closing and prv.isspace()) or (not closing and nxt.isspace()):
                return 0
            self.strike = not self.strike
            return 2
        if run >= 3 and self.bold == self.italic:
            want, used = ("bold", "italic"), 3
        elif run >= 2:
            want, used = ("bold",), 2
        else:
            want, used = ("italic",), 1
        closing = getattr(self, want[0])
        # underscores are left literal on purpose: in code talk they are
        # identifiers (__init__, snake_case) far more often than emphasis
        if (closing and prv.isspace()) or (not closing and nxt.isspace()):
            return 0
        for w in want:
            setattr(self, w, not closing)
        return used

    def link(self, s, i, line_end):
        """Render [text](url) starting at s[i]; return next index, 'wait', or None."""
        j = s.find("]", i + 1)
        if j == -1 or j + 1 >= len(s):
            return None if line_end or len(s) - i > 300 or "\n" in s[i:] else "wait"
        if s[j + 1] != "(":
            return None
        k = s.find(")", j + 2)
        if k == -1:
            return None if line_end or len(s) - i > 300 else "wait"
        text, url = s[i + 1:j], s[j + 2:k].strip()
        if not text or " " in url or not url:
            return None
        tag = self.new_id("link")
        self.insert(text, self.line_tags + self.style_tags() + ("link", tag))
        if re.match(r"https?://", url):
            self.tb.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
        self.prev = text[-1]
        return k + 1
