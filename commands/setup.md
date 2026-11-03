---
description: Teach Codex how to answer Claude — adds the codex-chat rule to ~/.codex/AGENTS.md
---

Set up the Codex side of codex-chat for this user.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/codex-chat/codex_chat.py" setup` and show the user the block it would add to `~/.codex/AGENTS.md`, in their language.
2. Ask the user whether to write it. Only on a clear yes, run the same command with `--write`.
3. Tell the user that new Codex chats pick the rule up on their own, and that a chat already running needs to be told to reread `~/.codex/AGENTS.md`.

The rule is wrapped in `claude-codex-bridge` markers, so running this again replaces the block instead of adding a second one. `CODEX_CHAT_LANG=en|ru` before the command picks the rule's language.
