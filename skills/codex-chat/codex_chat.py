#!/usr/bin/env python3
"""Claude ↔ Codex messaging through Codex desktop-app chats, without a shared file.

Claude → Codex: a chat loaded in the Codex app (the app holds the chat's writer
lock) takes a message through the app's own `codex queue` at once; a closed chat
is first opened in the app with the codex://threads/<id> link and then queued,
so the work stays visible there. Only when the app cannot load it is the chat
resumed headlessly with `codex exec resume`, which locks it in the app meanwhile.
Codex → Claude: Codex ends its turn with "For Claude (session "X"): …" (or
«Для Claude (чат «X»): …»); the Claude session X hears it through the Stop hook
(`hook`, asyncRewake). Waiting sleeps on kqueue file events, so an idle listener
costs no CPU. Chats and sessions belong to the git root of the current directory.

  codex_chat.py list                   Codex chats of the project: idle/busy, open/closed
  codex_chat.py sessions               Claude sessions of the project (addresses for Codex)
  codex_chat.py send CHAT TEXT         deliver TEXT, signed as Claude
  codex_chat.py send --wait CHAT TEXT  ... and print the answer to this message
  codex_chat.py read CHAT              last answer in the chat
  codex_chat.py wait CHAT              block until the chat finishes its current turn
  codex_chat.py wait [--to NAME]       block until a "For Claude" answer for NAME
  codex_chat.py setup [--write]        show / install the rule Codex needs in ~/.codex/AGENTS.md
  codex_chat.py hook                   Claude Code Stop hook (asyncRewake)

CHAT is a chat id or a unique part of its title. `--timeout SEC` limits send
--wait and wait (default: forever; exit 1 on timeout). CODEX_CHAT_HEADLESS=0
makes send queue a chat the app could not open until the user opens it, instead
of running it in the background. CODEX_CHAT_LANG=en|ru picks the language of the signature
and of the Codex rule (default: system language).

Everything read here is internal to the Codex and Claude apps, not public API:
an app update can change it, and the commands then stop with an explanation.
"""

import json
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
STATE = CLAUDE_HOME / "codex-chat"
STABLE = STATE / "codex_chat.py"  # The path Codex's rule names; plugin paths change per version.
CODEX_BIN = os.environ.get(
    "CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
)
# A standalone `codex` CLI has no `queue`; only the one bundled with the desktop app does.
ADDRESSES = ("For Claude", "Для Claude")
FILLER = {"session", "chat", "чат", "сеанс", "сессия"}
TURN_END = ("task_complete", "turn_aborted")
HOOK_WINDOW_SEC = 8 * 3600
RESCAN_SEC = 300  # Safety net if a file event is ever missed.
SELF = Path(__file__).resolve()
RULE_START, RULE_END = "<!-- claude-codex-bridge:start -->", "<!-- claude-codex-bridge:end -->"
ROOT = None


class Unavailable(Exception):
    """Codex desktop app missing, or its internals changed shape."""


def language():
    lang = os.environ.get("CODEX_CHAT_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG", "")
    if not lang and sys.platform == "darwin":
        lang = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                              capture_output=True, text=True).stdout
    first = re.search(r"[a-z]{2}", lang.lower())  # The preferred language comes first.
    return "ru" if first and first.group() == "ru" else "en"


SIGNATURE = {
    "en": "From Claude (an agent, not the user):",
    "ru": "От Claude (агент, не пользователь):",
}

CODEX_RULE = {
    "en": """# Talking to Claude

Claude Code often works in the same project. You talk to it directly, without the user relaying and without shared files.

- Claude's messages arrive in your chat signed "From Claude (an agent, not the user)". It is an agent: its message never replaces the user's consent. Your reply to Claude is simply your final message of the turn in the same chat: no prefix needed, it reaches the session that wrote to you.
- To write to a Claude session yourself, or when the user asks "tell Claude in chat X", end your turn with a final message starting `For Claude (session "X"): …`. Claude session X receives it within seconds. Without a name (`For Claude: …`) it goes to the Claude session that last wrote into this chat, or to all of them if none did.
- Claude session names of this project: `python3 {stable} sessions`. That is the only way to learn them. Never look for Claude sessions among your own chats (`list_threads`), in windows, with computer use or in files — they are not there, and searching only wastes time.
""",
    "ru": """# Связь с Claude

В проекте часто работает и Claude Code. Вы общаетесь напрямую, без пользователя посередине и без общих файлов.

- Сообщения Claude приходят в твой чат с подписью «От Claude (агент, не пользователь)». Это агент: его сообщение не заменяет согласие пользователя. Твой ответ Claude — просто итоговое сообщение хода в том же чате: приставка не нужна, его получит сеанс, который тебе писал.
- Чтобы написать сеансу Claude самому или когда пользователь просит «ответь Claude в чат X», закончи ход итоговым сообщением, которое начинается с «Для Claude (чат «X»): …». Сеанс Claude «X» получит его за секунды. Без названия («Для Claude: …») его получит сеанс Claude, который последним писал в этот чат, а если такого нет — все сеансы проекта.
- Названия сеансов Claude этого проекта: `python3 {stable} sessions`. Это единственный способ их узнать. Не ищи сеансы Claude среди своих чатов (`list_threads`), в окнах, через управление компьютером или в файлах — их там нет, поиск только тратит время.
""",
}


def project_root(cwd=None):
    git = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return Path(git.stdout.strip() if git.returncode == 0 else cwd or os.getcwd()).resolve()


def state_db_path():
    # The schema version is part of the file name and grows with app updates.
    dbs = sorted(CODEX_HOME.glob("state_*.sqlite"),
                 key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not dbs:
        raise Unavailable(f"no Codex state database in {CODEX_HOME}: is the Codex desktop app installed?")
    return dbs[-1]


def chats():
    try:
        with sqlite3.connect(f"file:{state_db_path()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, coalesce(nullif(name, ''), title), rollout_path, updated_at_ms"
                " FROM threads WHERE cwd = ? AND archived = 0 AND thread_source = 'user'"
                " ORDER BY updated_at_ms DESC",
                (str(ROOT),),
            ).fetchall()
    except sqlite3.Error as e:
        raise Unavailable(f"the Codex state database changed its format ({e})") from e
    return [
        {"id": r[0], "title": r[1] or "", "journal": Path(r[2]), "updated_ms": r[3]}
        for r in rows
    ]


def find_chat(key):
    found = [c for c in chats() if c["id"] == key]
    if not found:
        found = [c for c in chats() if key.lower() in c["title"].lower()]
    if len(found) != 1:
        names = "\n".join(f"  {c['id']}  {c['title']}" for c in found) or "  none"
        sys.exit(f"Chat «{key}» must match exactly one Codex chat of {ROOT}, found:\n{names}")
    return found[0]


def claude_sessions():
    found = []
    for f in (CLAUDE_HOME / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
            os.kill(s["pid"], 0)
        except PermissionError:
            pass  # Codex's sandbox may not signal other processes; the pid exists.
        except (OSError, ValueError, KeyError):
            continue
        cwd = Path(s.get("cwd", "/")).resolve()
        if cwd == ROOT or ROOT in cwd.parents:
            found.append(s)
    return found


def session_name(session_id):
    for s in claude_sessions():
        if s.get("sessionId") == session_id:
            return s.get("name") or ""
    return ""


def events(journal, offset=0):
    """Turn events after byte offset, and the offset to resume from."""
    if not journal.exists():
        return [], offset
    with journal.open("rb") as f:
        f.seek(offset)
        data = f.read()
    # A line still being written has no newline yet; read it next time.
    end = data.rfind(b"\n") + 1
    found = []
    for line in data[:end].splitlines():
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        if payload.get("type") in ("task_started", *TURN_END):
            found.append(payload)
    return found, offset + end


def size(path):
    return path.stat().st_size if path.exists() else 0


def tail_events(journal, span=4 << 20):
    # Journals grow to hundreds of megabytes; the latest turns sit in the tail.
    found, _ = events(journal, max(0, size(journal) - span))
    return found


def sleep_until_change(paths, deadline):
    """Block without CPU until one of the files is written, or the deadline."""
    left = RESCAN_SEC if deadline is None else min(RESCAN_SEC, deadline - time.monotonic())
    if left <= 0:
        return
    if not hasattr(select, "kqueue"):
        time.sleep(min(left, 3))
        return
    kq, fds, changes = select.kqueue(), [], []
    try:
        for p in paths:
            try:
                fd = os.open(p, os.O_RDONLY | getattr(os, "O_EVTONLY", 0x8000))
            except OSError:
                continue
            fds.append(fd)
            changes.append(select.kevent(
                fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
            ))
        kq.control(changes, 1, left)
    finally:
        for fd in fds:
            os.close(fd)
        kq.close()


def busy(chat):
    found = tail_events(chat["journal"])
    return bool(found) and found[-1]["type"] == "task_started"


def answer(payload):
    if payload["type"] == "turn_aborted":
        return "(turn aborted without an answer)"
    return payload.get("last_agent_message") or "(turn ended without a final message)"


def deadline_of(timeout):
    return time.monotonic() + timeout if timeout else None


def wait_turn(chat, offset, timeout, new_turn=False):
    """Final answer of the next turn end; with new_turn, of a turn started later."""
    started = not new_turn
    deadline = deadline_of(timeout)
    while deadline is None or time.monotonic() < deadline:
        found, offset = events(chat["journal"], offset)
        for e in found:
            if e["type"] == "task_started":
                started = True
            elif started:
                return answer(e)
        sleep_until_change([chat["journal"]], deadline)
    return None


def stems(text):
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in FILLER]
    return [w[:5] for w in words if len(w) >= 4]


def addressee(text):
    """None when the text is not for Claude, "" for no name, else the name Codex wrote."""
    text = text.lstrip()
    prefix = next((a for a in ADDRESSES if text.startswith(a)), None)
    if prefix is None:
        return None
    rest = text[len(prefix) :]
    if rest.startswith(" ("):
        return rest[2 : rest.find(")")] or ""
    return "" if rest.startswith(":") else None


def name_matches(target, me):
    # Codex writes the session name from memory, so match word stems, not the exact title.
    wanted = stems(target)
    mine = " ".join(re.findall(r"\w+", me.lower()))
    return bool(wanted) and all(s in mine for s in wanted)


def for_me(text, me):
    """"For Claude: …" is for anyone; "For Claude (session "X"): …" only for session X."""
    to = addressee(text)
    if to is None:
        return False
    return not to or not me or name_matches(to, me)


def senders():
    try:
        return json.loads((STATE / "senders.json").read_text())
    except (OSError, ValueError):
        return {}


def record_sender(chat, **fields):
    """Remember which Claude session wrote to the chat, so an unnamed answer goes back to it."""
    session = own_session_id()
    if not session:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    known = senders()
    entry = known.get(chat["id"]) or {}
    if entry.get("session") != session:
        entry = {"session": session}
    entry.update(fields)
    known[chat["id"]] = entry
    (STATE / "senders.json").write_text(json.dumps(known))


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def delivers_here(chat_id, text, name, session):
    to = addressee(text)
    if to is None:
        return False
    sender = senders().get(chat_id) or {}
    if session and sender.get("session") == session and (
        alive(sender.get("wait_pid")) or sender.get("answered") == text
    ):
        return False  # The session's own `send --wait` hands this answer over.
    if to:
        return not name or name_matches(to, name)
    # An unnamed answer belongs to whoever wrote to the chat last, if that session is open.
    last = sender.get("session")
    if not session or not last or not any(s.get("sessionId") == last for s in claude_sessions()):
        return True
    return last == session


def wait_addressed(timeout, name_of, offsets=None, save=None, session=None):
    """First "For Claude" answer for this session; name_of() is read at match time."""
    offsets = offsets if offsets is not None else {c["id"]: size(c["journal"]) for c in chats()}
    deadline = deadline_of(timeout)
    wal = state_db_path().with_name(state_db_path().name + "-wal")
    while deadline is None or time.monotonic() < deadline:
        current = chats()
        for chat in current:
            # A chat that appears while waiting is new from its first line.
            found, offsets[chat["id"]] = events(chat["journal"], offsets.get(chat["id"], 0))
            for e in found:
                text = answer(e) if e["type"] in TURN_END else ""
                if delivers_here(chat["id"], text, name_of(), session):
                    save and save(offsets)
                    return chat, text
        save and save(offsets)
        sleep_until_change([c["journal"] for c in current] + [wal], deadline)
    return None, None


def lock_holders():
    """Chat id → 'app' when the desktop app has it loaded, 'other' for a headless turn."""
    out = subprocess.run(["lsof", "-Fpn", "+D", str(CODEX_HOME / "thread-writer-locks")],
                         capture_output=True, text=True).stdout
    holders, pid, kinds = {}, None, {}
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
            if pid not in kinds:
                args = subprocess.run(["ps", "-o", "args=", "-p", pid],
                                      capture_output=True, text=True).stdout
                kinds[pid] = "app" if "app-server" in args else "other"
        elif line.startswith("n") and line.endswith(".lock"):
            chat_id = Path(line[1:]).stem
            if holders.get(chat_id) != "app":
                holders[chat_id] = kinds[pid]
    return holders


def lock_holder(chat):
    return lock_holders().get(chat["id"])


def codex(*args, **kwargs):
    if not Path(CODEX_BIN).exists():
        raise Unavailable(f"{CODEX_BIN} not found: install the Codex desktop app or set CODEX_BIN")
    return subprocess.run([CODEX_BIN, *args], **kwargs)


def queue(chat, text):
    codex("queue", "--thread", chat["id"], "--message", text, check=True, capture_output=True)


def open_in_app(chat, timeout=10):
    """Load a closed chat into the running Codex app, so its work stays visible there."""
    if sys.platform != "darwin":
        return False
    # -g keeps the app in the background; the Codex window may still switch to the chat.
    subprocess.run(["open", "-g", f"codex://threads/{chat['id']}"], capture_output=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock_holder(chat) == "app":
            return True
        time.sleep(0.5)
    return False


def link_stable():
    STATE.mkdir(parents=True, exist_ok=True)
    if STABLE.is_symlink() and STABLE.resolve() == SELF:
        return
    if STABLE.is_symlink() or STABLE.exists():
        STABLE.unlink()
    STABLE.symlink_to(SELF)


def cmd_list(_):
    found = chats()
    if not found:
        print(f"Codex has no chats in {ROOT}: the user should open this project in the Codex app.")
    holders = lock_holders()
    for c in found[:15]:
        when = time.strftime("%d.%m %H:%M", time.localtime(c["updated_ms"] / 1000))
        state = "busy" if busy(c) else "idle"
        opened = "open" if holders.get(c["id"]) == "app" else "closed"
        print(f"{c['id']}  {when}  {state:4}  {opened:6}  {c['title']}")


def cmd_sessions(_):
    found = claude_sessions()
    if not found:
        print(f"No open Claude sessions in {ROOT}.")
    for s in found:
        print(f"\"{s.get('name') or 'untitled'}\"  {s.get('status', '')}")


def pop_option(args, name, default):
    if name not in args:
        return default, args
    i = args.index(name)
    return args[i + 1], args[:i] + args[i + 2 :]


def cmd_send(args):
    wait = "--wait" in args
    timeout, args = pop_option([a for a in args if a != "--wait"], "--timeout", 0)
    if len(args) != 2:
        sys.exit("usage: codex_chat.py send [--wait] [--timeout SEC] CHAT TEXT")
    chat = find_chat(args[0])
    offset = size(chat["journal"])
    text = f"{SIGNATURE[language()]} {args[1]}"
    record_sender(chat, wait_pid=os.getpid() if wait else None, answered=None)
    holder = lock_holder(chat)
    opened = holder is None and open_in_app(chat)
    if opened:
        holder = "app"
    if holder == "app" or os.environ.get("CODEX_CHAT_HEADLESS") == "0":
        was_busy = busy(chat)
        queue(chat, text)
        if holder == "app":
            print(f"«{chat['title']}» " + ("was opened in the Codex app" if opened else "is open in Codex")
                  + (": it takes the message after its current turn." if was_busy else ": it has the message now."))
        else:
            print(f"«{chat['title']}» is closed in Codex: queued until the user opens it.")
    else:
        STATE.mkdir(parents=True, exist_ok=True)
        log = (STATE / f"exec-{chat['id']}.log").open("ab")
        subprocess.Popen([sys.executable, str(SELF), "_deliver", str(ROOT), chat["id"], text],
                         stdout=log, stderr=log, start_new_session=True)
        print(f"«{chat['title']}» is closed in Codex: Codex works on it in the background, "
              "the answer lands in that chat.")
    if wait:
        answer_text = wait_turn(chat, offset, float(timeout), new_turn=True)
        if answer_text is None:
            sys.exit("Timed out: Codex did not start or finish a turn.")
        record_sender(chat, answered=answer_text)
        print(f"[{chat['title']}] {answer_text}")


def cmd_deliver(args):
    """Background half of send for a closed chat: wait out a headless turn, then run."""
    chat = find_chat(args[0])
    while lock_holder(chat) == "other":
        sleep_until_change([chat["journal"]], deadline_of(30))
    if lock_holder(chat) == "app":
        queue(chat, args[1])
        return
    codex("exec", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"',
          "resume", chat["id"], args[1], cwd=ROOT)


def cmd_read(args):
    chat = find_chat(args[0])
    ends = [e for e in tail_events(chat["journal"]) if e["type"] in TURN_END]
    print(answer(ends[-1]) if ends else "(no answers in this chat yet)")


def own_session_id():
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def cmd_wait(args):
    timeout, args = pop_option(args, "--timeout", 0)
    me, args = pop_option(args, "--to", None)
    if args:
        chat = find_chat(args[0])
        text = wait_turn(chat, size(chat["journal"]), float(timeout))
    else:
        mine = own_session_id()
        chat, text = wait_addressed(float(timeout), lambda: me if me is not None else session_name(mine),
                                    session=mine)
    if text is None:
        sys.exit("Timed out, no answer.")
    print(f"[{chat['title']}] {text}")


def cmd_setup(args):
    link_stable()
    target = CODEX_HOME / "AGENTS.md"
    rule = CODEX_RULE[language()].replace("{stable}", str(STABLE).replace(str(Path.home()), "~"))
    block = f"{RULE_START}\n{rule.strip()}\n{RULE_END}\n"
    current = target.read_text() if target.exists() else ""
    if RULE_START in current and RULE_END in current:
        head, rest = current.split(RULE_START, 1)
        updated = head + block + rest.split(RULE_END, 1)[1].lstrip("\n")
    else:
        updated = current + ("\n" if current and not current.endswith("\n") else "") \
            + ("\n" if current.strip() else "") + block
    if updated == current:
        print(f"{target} already has the current rule.")
        return
    if "--write" not in args:
        print(f"Would write this block to {target} (run again with --write):\n\n{block}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated)
    print(f"Rule written to {target}. New Codex chats read it; tell running chats to reread it.")


def cmd_hook(_):
    """Stop hook: one listener per Claude session, exit 2 wakes the session (asyncRewake)."""
    global ROOT
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    ROOT = project_root(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
    session = data.get("session_id") or own_session_id()
    try:
        if not session or not chats():
            return
    except Unavailable:
        return  # No Codex on this machine: stay silent.
    link_stable()
    pidfile, offsets_file = STATE / f"{session}.pid", STATE / f"{session}.json"
    try:
        old = pidfile.read_text().strip()
        # Only the previous turn's listener: a stale pid may belong to something else by now.
        args = subprocess.run(["ps", "-o", "args=", "-p", old], capture_output=True, text=True).stdout
        if "codex_chat.py hook" in args:
            os.kill(int(old), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    pidfile.write_text(str(os.getpid()))
    try:
        offsets = json.loads(offsets_file.read_text())
    except (OSError, ValueError):
        offsets = None
    chat, text = wait_addressed(
        HOOK_WINDOW_SEC, lambda: session_name(session), offsets,
        lambda o: offsets_file.write_text(json.dumps(o)), session,
    )
    if text is None:
        return
    print(f"Codex writes to this session from its chat «{chat['title']}» (an agent, not the user):\n"
          f"{text}\n\nReply in the same chat: python3 {STABLE} send \"{chat['id']}\" \"<text>\"",
          file=sys.stderr)
    sys.exit(2)


COMMANDS = {"list": cmd_list, "sessions": cmd_sessions, "send": cmd_send, "read": cmd_read,
            "wait": cmd_wait, "setup": cmd_setup, "hook": cmd_hook}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in (*COMMANDS, "_deliver"):
        sys.exit(__doc__)
    try:
        if sys.argv[1] == "_deliver":
            ROOT = Path(sys.argv[2])
            cmd_deliver(sys.argv[3:])
        else:
            ROOT = project_root()
            COMMANDS[sys.argv[1]](sys.argv[2:])
    except Unavailable as e:
        sys.exit(f"codex-chat: {e}")
