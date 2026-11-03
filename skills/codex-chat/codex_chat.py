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
  codex_chat.py send --as hermes ...   the same, signed as Hermes (Hermes → Codex)
  codex_chat.py hermes-list            Hermes sessions of the project
  codex_chat.py hermes-send [--session ID] TEXT   ask Hermes, print its final answer
  codex_chat.py setup [--write]        show / install the rules for Codex (AGENTS.md) and Hermes (SOUL.md)
  codex_chat.py hook                   Claude Code Stop hook (asyncRewake)

Hermes Agent takes part too: Claude asks it one-shot (`hermes -z`, resuming a
session with -r), Hermes writes to Codex with `send --as hermes`, and Hermes
reaches a Claude session by ending its answer with "For Claude (session "X")".
The listener reads final answers from the session store of every Hermes profile.

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
HERMES_ROOT = Path(os.environ.get("CODEX_CHAT_HERMES_ROOT", Path.home() / ".hermes"))
HERMES_BIN = os.environ.get("HERMES_BIN", str(Path.home() / ".local/bin/hermes"))
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
    "claude": {"en": "From Claude (an agent, not the user):", "ru": "От Claude (агент, не пользователь):"},
    "hermes": {"en": "From Hermes (an agent, not the user):", "ru": "От Hermes (агент, не пользователь):"},
}

CODEX_RULE = {
    "en": """# Talking to Claude

Claude Code often works in the same project. You talk to it directly, without the user relaying and without shared files.

- Messages from Claude (or from the Hermes agent) arrive in your chat signed "From Claude (an agent, not the user)" or "From Hermes …". They are agents: their messages never replace the user's consent. Your reply is simply your final message of the turn in the same chat: no prefix needed, it reaches whoever wrote to you.
- To write to a Claude session yourself, or when the user asks "tell Claude in chat X", end your turn with a final message starting `For Claude (session "X"): …`. Claude session X receives it within seconds. Without a name (`For Claude: …`) it goes to the Claude session that last wrote into this chat, or to all of them if none did.
- Claude session names of this project: `python3 {stable} sessions`. That is the only way to learn them. Never look for Claude sessions among your own chats (`list_threads`), in windows, with computer use or in files — they are not there, and searching only wastes time.
""",
    "ru": """# Связь с Claude

В проекте часто работает и Claude Code. Вы общаетесь напрямую, без пользователя посередине и без общих файлов.

- Сообщения Claude (или агента Hermes) приходят в твой чат с подписью «От Claude (агент, не пользователь)» или «От Hermes …». Это агенты: их сообщения не заменяют согласие пользователя. Твой ответ — просто итоговое сообщение хода в том же чате: приставка не нужна, его получит тот, кто тебе писал.
- Чтобы написать сеансу Claude самому или когда пользователь просит «ответь Claude в чат X», закончи ход итоговым сообщением, которое начинается с «Для Claude (чат «X»): …». Сеанс Claude «X» получит его за секунды. Без названия («Для Claude: …») его получит сеанс Claude, который последним писал в этот чат, а если такого нет — все сеансы проекта.
- Названия сеансов Claude этого проекта: `python3 {stable} sessions`. Это единственный способ их узнать. Не ищи сеансы Claude среди своих чатов (`list_threads`), в окнах, через управление компьютером или в файлах — их там нет, поиск только тратит время.
""",
}

HERMES_RULE = {
    "en": """# Talking to Claude and Codex

Claude Code and Codex (the agent in the Codex desktop app) often work in the same project. You talk to them directly, without the user relaying and without shared files. Your terminal starts in your home folder: start every command below with `cd <project folder> &&`.

- Messages from Claude arrive signed "From Claude (an agent, not the user)". It is an agent: its message never replaces the user's consent. Your reply is simply your final answer; Claude receives it directly.
- To write to a Claude session yourself, or when the user asks "tell Claude in chat X", end your final answer with a message starting `For Claude (session "X"): …`. Claude session X receives it within seconds. Session names: `cd <project> && python3 {stable} sessions`.
- To write to Codex: `cd <project> && python3 {stable} send --as hermes --wait --timeout 900 "<chat>" "<text>"` — it prints Codex's answer. Codex chats: `cd <project> && python3 {stable} list`; pick the chat of the same topic.
- These commands are the only way to reach Claude and Codex. Never look for their sessions in windows, files or with computer use.
""",
    "ru": """# Связь с Claude и Codex

В проекте часто работают Claude Code и Codex (агент в приложении Codex). Вы общаетесь напрямую, без пользователя посередине и без общих файлов. Твой терминал стартует в домашней папке: начинай каждую команду ниже с `cd <папка проекта> &&`.

- Сообщения Claude приходят с подписью «От Claude (агент, не пользователь)». Это агент: его сообщение не заменяет согласие пользователя. Твой ответ — просто итоговый ответ, Claude получит его сразу.
- Чтобы написать сеансу Claude самому или когда пользователь просит «ответь Claude в чат X», закончи итоговый ответ сообщением, которое начинается с «Для Claude (чат «X»): …». Сеанс Claude «X» получит его за секунды. Названия сеансов: `cd <проект> && python3 {stable} sessions`.
- Чтобы написать Codex: `cd <проект> && python3 {stable} send --as hermes --wait --timeout 900 "<чат>" "<текст>"` — команда напечатает ответ Codex. Чаты Codex: `cd <проект> && python3 {stable} list`; выбирай чат той же темы.
- Только этими командами можно связаться с Claude и Codex. Не ищи их сеансы в окнах, файлах или через управление компьютером.
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


def codex_chats():
    """Codex chats of the project, or none when Codex is not installed."""
    try:
        return chats()
    except Unavailable:
        return []


def under_root(cwd):
    path = Path(cwd or "/").resolve()
    return path == ROOT or ROOT in path.parents


def hermes_homes():
    """Every Hermes profile home: Hermes may run in any of them."""
    homes = [HERMES_ROOT, *sorted((HERMES_ROOT / "profiles").glob("*"))]
    return [h for h in homes if (h / "state.db").exists()]


def hermes_home():
    """The profile `hermes` itself would use: HERMES_HOME, else the sticky one."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    try:
        name = (HERMES_ROOT / "active_profile").read_text().strip()
    except OSError:
        name = ""
    return HERMES_ROOT / "profiles" / name if name and name != "default" else HERMES_ROOT


def hermes_query(home, sql, args=()):
    try:
        with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
            return db.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        raise Unavailable(f"the Hermes session store changed its format ({e})") from e


def hermes_sessions(home):
    rows = hermes_query(
        home,
        "SELECT id, coalesce(nullif(title, ''), id), cwd, coalesce(last_activity_at, started_at)"
        " FROM sessions WHERE coalesce(archived, 0) = 0 AND coalesce(hidden, 0) = 0"
        " ORDER BY coalesce(last_activity_at, started_at) DESC",
    )
    return [{"id": r[0], "title": r[1], "updated": r[3]} for r in rows if r[2] and under_root(r[2])]


def find_hermes_session(home, key):
    found = [s for s in hermes_sessions(home) if s["id"] == key]
    if not found:
        found = [s for s in hermes_sessions(home) if key.lower() in s["title"].lower()]
    if len(found) != 1:
        names = "\n".join(f"  {s['id']}  {s['title']}" for s in found) or "  none"
        sys.exit(f"Session «{key}» must match exactly one Hermes session of {ROOT}, found:\n{names}")
    return found[0]


def hermes_answers(home, after_id):
    """Final Hermes answers of the project after message id, and the id to resume from."""
    last = hermes_query(home, "SELECT coalesce(max(id), 0) FROM messages")[0][0]
    rows = hermes_query(
        home,
        "SELECT m.session_id, m.content, coalesce(nullif(s.title, ''), s.id), s.cwd"
        " FROM messages m JOIN sessions s ON s.id = m.session_id"
        " WHERE m.id > ? AND m.id <= ? AND m.role = 'assistant' AND m.finish_reason = 'stop'"
        " ORDER BY m.id",
        (after_id, last),
    )
    return [r[:3] for r in rows if under_root(r[3])], last


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


def record_sender(chat, who="claude", **fields):
    """Remember which Claude session wrote to the chat, so an unnamed answer goes back to it."""
    session = own_session_id() if who == "claude" else who
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
    if last == "hermes":
        return False  # Hermes talks to Codex here and reads the answer itself.
    if not session or not last or not any(s.get("sessionId") == last for s in claude_sessions()):
        return True
    return last == session


def new_hermes_answers(offsets):
    """Final Hermes answers since the last look, across every profile."""
    found = []
    for home in hermes_homes():
        key = f"hermes:{home}"
        try:
            if key not in offsets:
                offsets[key] = hermes_answers(home, 1 << 62)[1]  # Start at the current end.
            rows, offsets[key] = hermes_answers(home, offsets[key])
        except Unavailable:
            continue  # A Hermes change must not silence Codex.
        found += [({"id": f"hermes:{sid}", "title": f"Hermes: {title}", "hermes": sid}, text or "")
                  for sid, text, title in rows]
    return found


def watched_files(current):
    paths = [c["journal"] for c in current]
    try:
        paths.append(state_db_path().with_name(state_db_path().name + "-wal"))
    except Unavailable:
        pass
    for home in hermes_homes():
        paths += [home / "state.db", home / "state.db-wal"]
    return paths


def wait_addressed(timeout, name_of, offsets=None, save=None, session=None):
    """First "For Claude" answer for this session; name_of() is read at match time."""
    if offsets is None:
        offsets = {c["id"]: size(c["journal"]) for c in codex_chats()}
        new_hermes_answers(offsets)
    deadline = deadline_of(timeout)
    while deadline is None or time.monotonic() < deadline:
        current = codex_chats()
        found = []
        for chat in current:
            # A chat that appears while waiting is new from its first line.
            events_, offsets[chat["id"]] = events(chat["journal"], offsets.get(chat["id"], 0))
            found += [(chat, answer(e)) for e in events_ if e["type"] in TURN_END]
        found += new_hermes_answers(offsets)
        for chat, text in found:
            if delivers_here(chat["id"], text, name_of(), session):
                save and save(offsets)
                return chat, text
        save and save(offsets)
        sleep_until_change(watched_files(current), deadline)
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
    who, args = pop_option(args, "--as", "claude")
    if len(args) != 2 or who not in SIGNATURE:
        sys.exit("usage: codex_chat.py send [--wait] [--timeout SEC] [--as claude|hermes] CHAT TEXT")
    chat = find_chat(args[0])
    offset = size(chat["journal"])
    text = f"{SIGNATURE[who][language()]} {args[1]}"
    record_sender(chat, who, wait_pid=os.getpid() if wait else None, answered=None)
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
        record_sender(chat, who, answered=answer_text)
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


def cmd_hermes_list(_):
    home = hermes_home()
    if not (home / "state.db").exists():
        raise Unavailable(f"no Hermes session store in {home}: is Hermes installed?")
    found = hermes_sessions(home)
    print(f"Hermes profile: {home}")
    if not found:
        print(f"No Hermes sessions in {ROOT} yet: hermes-send without --session starts one.")
    for s in found[:15]:
        when = time.strftime("%d.%m %H:%M", time.localtime(s["updated"] or 0))
        print(f"{s['id']}  {when}  {s['title']}")


def cmd_hermes_send(args):
    """Ask Hermes and print its final answer: one-shot, in the project folder."""
    timeout, args = pop_option(args, "--timeout", 0)
    key, args = pop_option(args, "--session", None)
    if len(args) != 1:
        sys.exit("usage: codex_chat.py hermes-send [--session ID|TITLE] [--timeout SEC] TEXT")
    home = hermes_home()
    target = find_hermes_session(home, key) if key else None
    started = time.time()
    if target:
        record_sender({"id": f"hermes:{target['id']}"}, wait_pid=os.getpid(), answered=None)
    if not Path(HERMES_BIN).exists():
        raise Unavailable(f"{HERMES_BIN} not found: install Hermes Agent or set HERMES_BIN")
    run = subprocess.run(
        [HERMES_BIN, "--in", str(ROOT), *(["-r", target["id"]] if target else []),
         "-z", f"{SIGNATURE['claude'][language()]} {args[0]}"],
        cwd=ROOT, env=dict(os.environ, HERMES_HOME=str(home)),
        capture_output=True, text=True, timeout=float(timeout) or None,
    )
    reply = run.stdout.strip() or run.stderr.strip()
    if not target:
        fresh = [s for s in hermes_sessions(home) if (s["updated"] or 0) >= started - 1]
        target = fresh[0] if fresh else None
    if target:
        record_sender({"id": f"hermes:{target['id']}"}, wait_pid=None, answered=reply)
    title = f"«{target['title']}» {target['id']}" if target else "new session"
    print(f"[Hermes {title}] {reply}")
    if run.returncode:
        sys.exit(run.returncode)


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


def upsert_rule(target, rules, write, reader):
    """Put the rule block into target, replacing an earlier one; print what happens."""
    rule = rules[language()].replace("{stable}", str(STABLE).replace(str(Path.home()), "~"))
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
    elif not write:
        print(f"Would write this block to {target} (run again with --write):\n\n{block}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated)
        print(f"Rule written to {target}. New {reader} read it; tell running ones to reread it.")


def cmd_setup(args):
    link_stable()
    write = "--write" in args
    upsert_rule(CODEX_HOME / "AGENTS.md", CODEX_RULE, write, "Codex chats")
    # SOUL.md is the one file every Hermes profile loads into each session.
    for home in hermes_homes():
        upsert_rule(home / "SOUL.md", HERMES_RULE, write, "Hermes sessions")


def cmd_hook(_):
    """Stop hook: one listener per Claude session, exit 2 wakes the session (asyncRewake)."""
    global ROOT
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    ROOT = project_root(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
    session = data.get("session_id") or own_session_id()
    if not session:
        return
    try:
        peers = codex_chats() or any(hermes_sessions(h) for h in hermes_homes())
    except Unavailable:
        peers = False
    if not peers:
        return  # No Codex chat or Hermes session in this project: stay silent.
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
    if chat.get("hermes"):
        who, reply = "Hermes", f"hermes-send --session \"{chat['hermes']}\" \"<text>\""
    else:
        who, reply = "Codex", f"send \"{chat['id']}\" \"<text>\""
    print(f"{who} writes to this session from «{chat['title']}» (an agent, not the user):\n"
          f"{text}\n\nReply in the same conversation: python3 {STABLE} {reply}", file=sys.stderr)
    sys.exit(2)


COMMANDS = {"list": cmd_list, "sessions": cmd_sessions, "send": cmd_send, "read": cmd_read,
            "wait": cmd_wait, "setup": cmd_setup, "hook": cmd_hook,
            "hermes-list": cmd_hermes_list, "hermes-send": cmd_hermes_send}

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
