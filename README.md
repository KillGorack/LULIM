# LULIM

A small desktop chat app for local LLMs running in [Ollama](https://ollama.com),
built with Python and [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter).

![icon](icon.png)

## Features

- Streams replies token by token from your local Ollama server
- Lists every model you have installed in Ollama and lets you switch between them
  (the window title shows the current model)
- Renders Markdown as it arrives: code blocks, bold/italic, lists, quotes, headings and links
- Shows the reasoning of thinking models (Ollama's `thinking` field or `<think>` tags)
  separately from the answer, with how long the model thought
- Shows tokens and tokens/sec for each reply
- Saves chats automatically and lists them in the sidebar; right-click a chat to delete it
- Right-click your own messages to copy them
- Dark and light themes
- Unloads the model from memory when you close the window

## Requirements

- Python 3.10+ with Tk
- [Ollama](https://ollama.com) running locally on the default port (`11434`)
  with at least one model pulled
- Python packages: `customtkinter`, `requests`, `pillow`

On Fedora, Tk is a separate package:

```bash
sudo dnf install python3-tkinter
```

## Install

```bash
git clone https://github.com/KillGorack/LULIM.git
cd LULIM
pip install --user customtkinter requests pillow
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
- **New Chat** starts a new conversation

## Configuration

These settings are constants at the top of `main.py`:

| Constant   | Default                                 | What it is                          |
|------------|-----------------------------------------|-------------------------------------|
| `MODEL`    | `qwen2.5-coder:7b`                      | Model used if no other is chosen   |
| `URL`      | `http://localhost:11434/api/chat`       | Ollama chat endpoint                |
| `CHAT_DIR` | `~/.local/share/quen/chats`             | Where chats are saved               |

Your last model and theme are remembered in `~/.local/share/quen/settings.json`.

## Files

- `main.py` is the window, the sidebar, the Ollama connection and chat saving
- `render.py` is the streaming Markdown renderer for replies
- `icon.png` is the window icon

## License

MIT, see [LICENSE](LICENSE).
