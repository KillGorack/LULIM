# LULIM

A small desktop chat app for local LLMs running in [Ollama](https://ollama.com),
built with Python and [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter).

<img width="1110" height="597" alt="image" src="https://github.com/user-attachments/assets/f6d745c8-adba-4e41-952d-c2e44adb8cc3" />

## Features

- Streams replies token by token from your local Ollama server
- Stop a reply mid-stream with the Stop button (or Esc); the partial reply is kept
- Lists every model you have installed in Ollama and lets you switch between them
  (the window title shows the current model)
- Renders Markdown as it arrives: code blocks, bold/italic, lists, quotes, headings and links
- Syntax highlighting in code blocks that name their language (needs the optional `pygments` package)
- Shows the reasoning of thinking models (Ollama's `thinking` field or `<think>` tags)
  separately from the answer, with how long the model thought
- Shows tokens, tokens/sec and how full the context window is for each reply
- Reopen any saved chat and keep going: the whole conversation is sent back to the model
- Saves chats automatically and lists them in the sidebar; right-click a chat to rename it
  or export it as a Markdown file (its images are copied to a folder next to it),
  click its trash can twice to delete it. The search box above the list filters chats
  by name and message text
- Copy any reply, or regenerate the latest one, from the links under it;
  a failed send (e.g. Ollama not running) gets a Retry link
- Right-click your own messages to copy them, or edit one and send it again
  (the chat is cut off at that message)
- Attach images for models that can see them (Ollama reports this per model, so the
  paperclip button only shows for those): pick files with the paperclip, or paste a
  screenshot or a copied image file with Ctrl+V. Click a picture in the chat to open it
- A system prompt for all chats (the scroll button in the toolbar), e.g. "be concise,
  best option first"; it's sent with every request but not saved into chats
- Dark and light themes
- Unloads the model from memory when you close the window

## Requirements

- Python 3.10+ with Tk
- [Ollama](https://ollama.com) running locally on the default port (`11434`)
  with at least one model pulled
- Python packages: `customtkinter`, `requests`, `pillow`
- Optional: `pygments` for syntax highlighting in code blocks
- Optional, for images: `wl-paste` (package `wl-clipboard`) to paste images on Wayland, and
  `kdialog` or `zenity` for the desktop's own file picker (otherwise Tk's basic one is used)

On Fedora, Tk is a separate package:

```bash
sudo dnf install python3-tkinter
```

## Install

```bash
git clone https://github.com/KillGorack/LULIM.git
cd LULIM
pip install --user customtkinter requests pillow
pip install --user pygments  # optional: syntax highlighting
```

Pull a model if you don't have one yet. The default is `qwen2.5-coder:7b`:

```bash
ollama pull qwen2.5-coder:7b
```

## Run

```bash
./main.py
```

or `python3 main.py`.

- **Enter** sends the message
- **Shift+Enter** adds a new line
- **Esc** stops a reply
- **Ctrl+N** starts a new chat
- **Ctrl+B** shows or hides the sidebar
- **Ctrl+F** searches your chats (Esc clears the search)
- **Ctrl +** / **Ctrl −** make the chat text bigger or smaller, **Ctrl+0** resets it

## Configuration

These settings are constants at the top of `main.py`:

| Constant   | Default                                 | What it is                          |
|------------|-----------------------------------------|-------------------------------------|
| `MODEL`    | `qwen2.5-coder:7b`                      | Model used if no other is chosen   |
| `URL`      | `http://localhost:11434/api/chat`       | Ollama chat endpoint                |
| `CHAT_DIR` | `~/.local/share/quen/chats`             | Where chats are saved; attached images go in `images/` inside it |
| `NUM_CTX`  | `16384`                                 | Context window in tokens, capped at the model's max |

Your last model and theme are remembered in `~/.local/share/quen/settings.json`.
You can also set `"num_ctx"` there to override `NUM_CTX`. A bigger context window
lets the model remember more of a long chat, but uses more memory (VRAM/RAM).

## Files

- `main.py` is the window, the sidebar, the Ollama connection and chat saving
- `render.py` is the streaming Markdown renderer for replies
- `icon.png` is the window icon
- `icons/` holds the toolbar and chat list icons from [Lucide](https://lucide.dev)
  (ISC license, see `icons/LICENSE`). They are white PNGs tinted to the theme at runtime;
  to add one, convert the Lucide SVG to a 96×96 white PNG and load it by name with `load_icon`

## License

MIT, see [LICENSE](LICENSE).
