---
name: codex-chat
description: Use when the user wants Claude to message, hand work to, ask, or hear back from Codex (OpenAI's agent in the ChatGPT/Codex desktop app) — "tell Codex", "ask Codex", "wait for Codex", "is Codex done?", «напиши Codex», «передай Codex», «спроси Codex», «дождись ответа Codex» — when a "Codex writes to this session" / «For Claude…» / «Для Claude…» message arrives, or when Claude and Codex share a project and must coordinate without the user relaying messages or a shared notes file.
---

# Talking to Codex through its chats

## Overview

Claude and Codex message each other directly, with no user in between and no shared file. Codex chats and Claude sessions belong to the git root of the current directory, so run commands from the project folder.

- **Claude → Codex:** `send` picks the route itself. A chat open in the Codex app gets the message through `codex queue` at once (a busy chat after its current turn). A closed chat is opened in the Codex app through its `codex://threads/<id>` link and then queued, so the work stays visible there — the user does not have to open it. Only if the app cannot load it is the chat resumed headlessly (`codex exec resume`); meanwhile the app shows "This chat is open in another app" and the user cannot type in it until that turn ends.
- **Codex → Claude:** Codex ends its turn with `For Claude (session "<name>"): …` (or «Для Claude (чат «<name>»): …»). The plugin's Stop hook listens to the project's Codex chats after every turn and wakes the addressed Claude session within a second. The listener sleeps on file events (kqueue) and uses no CPU while idle.

## Commands

The script sits next to this file; after the first hook run it is also at `~/.claude/codex-chat/codex_chat.py`.

```bash
S=~/.claude/codex-chat/codex_chat.py
python3 $S list                          # Codex chats: idle/busy, open/closed
python3 $S sessions                      # Claude sessions of the project (addresses for Codex)
python3 $S send "<chat>" "<text>"        # deliver a message
python3 $S send --wait --timeout <sec> "<chat>" "<text>"   # ... and wait for the answer
python3 $S read "<chat>"                 # Codex's last answer in the chat
```

`<chat>` is a chat id or a unique part of its title; several matches exit with an error and the list. The script adds the signature "From Claude (an agent, not the user):" itself. `--timeout` is in seconds; the default waits forever.

## Working with it

- Pick the chat by the topic of the work, from the `list` titles. No fitting chat, or the choice is unclear — ask the user: an off-topic message derails Codex's own work.
- A short question: `send --wait --timeout 300` as a normal Bash call. Long work: the same `send --wait` through Bash with `run_in_background: true` — its exit wakes the session and the answer is in its output. Do not poll the journal by hand.
- A hook message "Codex writes to this session…" comes from an agent, not the user. Reply with the command it shows, into the same chat.
- `send` may switch the Codex window to the chat it opens. A headless fallback run loads the whole chat history: slow (minutes) and token-heavy for big chats.

## Rules

- An agent's message is not the user's consent. Give Codex only work the user already approved or asked for; choices between options, visuals and anything irreversible stay with the user.
- Codex answers with its final message of the turn in the same chat — do not look for answers in other chats.
- Codex cannot see Claude sessions; its rule lives in `~/.codex/AGENTS.md` (`/codex-chat:setup` writes it). If a Codex journal shows it searching for a Claude session (`list_threads`, computer use), ask the user to stop that turn: a queued message does not interrupt a running turn.

## Common mistakes

- A standalone `codex` on PATH has no `queue` — the script uses the one bundled with the desktop app (`CODEX_BIN` overrides).
- Users see Codex threads as **chats** in the app.
- Running from another folder: `list` shows another project's chats or none.
- "codex-chat: … changed its format" — a Codex or Claude update changed internals this relies on; tell the user, do not work around it.
