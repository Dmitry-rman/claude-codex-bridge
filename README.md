# claude-codex-bridge

[Русская версия](README.ru.md)

A Claude Code plugin that lets **Claude Code** and **Codex** (the agent in the ChatGPT/Codex desktop app) talk to each other directly — no copy-pasting between windows, no shared notes file, no opening chats just to nudge an agent.

- **Claude → Codex.** Claude writes into the Codex chat that matches the topic. An open chat takes the message at once; a closed one is opened in the Codex app by itself, so you can watch the work.
- **Codex → Claude.** A reply needs nothing special: the Claude session that wrote to the chat gets it. To reach a particular session, Codex ends its turn with `For Claude (session "Name"): …`; that session wakes up about a second later and replies into the same chat. An unnamed `For Claude: …` goes to the session that last wrote into that chat.
- **No CPU while waiting.** The listener sleeps on macOS file events (kqueue) and wakes only when Codex writes to a chat.

Unofficial. Not affiliated with Anthropic or OpenAI.

## Requirements

- macOS.
- The Codex desktop app (ChatGPT.app with Codex) — its bundled CLI provides `codex queue`. A standalone `codex` CLI is not enough.
- Claude Code (CLI or the Claude desktop app's Code tab).
- `python3` and `git` on PATH (stock macOS developer tools are fine).

## Install

In Claude Code:

```
/plugin marketplace add Dmitry-rman/claude-codex-bridge
/plugin install codex-chat@claude-codex-bridge
/codex-chat:setup
```

`/codex-chat:setup` shows the rule Codex needs and, after you agree, adds it to `~/.codex/AGENTS.md` inside marker comments (running it again updates the block instead of duplicating it). New Codex chats read it automatically; tell a chat that is already running to reread `~/.codex/AGENTS.md`.

Start a new Claude session after installing so the Stop hook loads.

## Use

Just talk to Claude: "tell Codex in the credits chat that I picked option B and wait until it's done". Claude finds the chat, sends the message and is woken by Codex's answer.

In Codex: "tell Claude in the Menu session that the export is done". Codex lists the project's Claude sessions with one command and ends its turn with `For Claude (session "Menu"): …`.

The script can also be run by hand from a project folder:

```bash
python3 ~/.claude/codex-chat/codex_chat.py list       # Codex chats: idle/busy, open/closed
python3 ~/.claude/codex-chat/codex_chat.py sessions   # Claude sessions (addresses for Codex)
python3 ~/.claude/codex-chat/codex_chat.py send "credits" "Option B was chosen, apply it"
```

Settings (environment variables):

| Variable | Effect |
|---|---|
| `CODEX_CHAT_HEADLESS=0` | Never fall back to a background run, even if the app could not open the chat; the message waits until you open it. |
| `CODEX_CHAT_LANG=en\|ru` | Language of Claude's signature and of the Codex rule. Default: system language. |
| `CODEX_BIN` | Path to the Codex CLI with `queue` (default: the one inside ChatGPT.app). |

## How it works

- Codex keeps a writer lock for every chat the app has loaded (`~/.codex/thread-writer-locks`). If the app holds it, the message goes through `codex queue`. A closed chat is first opened in the app with the `codex://threads/<id>` link (the Codex window may switch to it), then queued. Only if the app cannot load it does the plugin fall back to `codex exec resume <chat>` in the background (sandbox `workspace-write`, no approval prompts); while that runs, the app shows the chat as open in another app.
- Every finished Codex turn is written to the chat's journal in `~/.codex/sessions`. Waiting for an answer means waiting for the next `task_complete` there.
- Claude session names come from `~/.claude/sessions/*.json`. `send` records which session last wrote to each chat (`~/.claude/codex-chat/senders.json`), so unnamed answers go back to it, and a session waiting with `send --wait` is not woken a second time by the hook.
- The plugin's `Stop` hook runs with `asyncRewake` after each Claude turn: one listener per session, the previous one is replaced, read positions are saved so nothing is lost in between. A matching message is printed and the hook exits with code 2, which wakes the session.

## Caveats

- **Internal formats.** Codex's state database, chat journals and lock files, and Claude's session files are not public APIs. An app update can break the plugin; it then stops with a clear message instead of guessing.
- **Headless fallback.** It loads the chat's whole history: for a long chat that can take minutes and a lot of Codex tokens, and the chat is locked in the app meanwhile. Set `CODEX_CHAT_HEADLESS=0` to never use it.
- **A running turn is not interrupted.** A message to a busy chat is handled after its current turn.
- **Privacy.** The listener reads the journals of the Codex chats of the current project, on your machine only. Nothing is sent anywhere.
- **Consent.** Messages between agents are signed as agent messages; both rules say they never replace the user's consent.
- The listener wakes a Claude session only while it is open. Messages that arrive while it is closed are still in Codex's chat (`read`).

## License

MIT
