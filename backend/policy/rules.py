"""Deterministic safety rules. Ordinary code decides whatever ordinary code can decide.

`classify` returns a verdict with `action=None` when no rule applies; only those
ambiguous calls are ever passed on to a decision provider such as Jev.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any

from pydantic import BaseModel

from backend.policy.actions import PolicyAction

HOME = "/home/agent"
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", "{", "}"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
DOWNLOADERS = {"curl", "wget", "fetch"}
# Wrappers that run another command; value = how many of their own arguments to skip.
WRAPPERS = {"nohup": 0, "time": 0, "nice": 0, "ionice": 0, "stdbuf": 0, "command": 0, "exec": 0,
            "builtin": 0, "timeout": 1, "watch": 0, "xargs": 0}
SUDO_OPTIONS_WITH_VALUE = {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}
# Commands that only read state. A command line built solely from these, with
# no output redirection, is allowed without asking anyone.
READ_ONLY = {
    "pwd", "ls", "cat", "echo", "printf", "whoami", "id", "hostname", "uname", "date", "df", "du",
    "free", "uptime", "ps", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "which", "type",
    "printenv", "stat", "file", "find", "tree", "lsb_release", "true", "false", "test", "[", "cd",
    "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath", "readlink", "nproc", "lscpu",
    "lsblk", "ss", "env", "less", "more", "diff", "md5sum", "sha256sum", "jq", "column",
}
PROTECTED_DIRS = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/var/lib", "/root",
                  "/dev", "/proc", "/sys", "/opt/agent-office")
SAFE_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"}
DISK_TOOLS = {"wipefs", "fdisk", "sfdisk", "parted", "blkdiscard", "shred"}
POWER_COMMANDS = {"shutdown", "reboot", "poweroff", "halt"}
USER_COMMANDS = {"useradd", "userdel", "usermod", "adduser", "deluser", "groupadd", "groupdel",
                 "groupmod", "passwd", "chpasswd", "visudo", "gpasswd", "chage"}
PACKAGE_REMOVALS = {
    "apt": {"remove", "purge", "autoremove", "autopurge"},
    "apt-get": {"remove", "purge", "autoremove", "autopurge"},
    "aptitude": {"remove", "purge"},
    "dpkg": {"-r", "-P", "--remove", "--purge"},
    "snap": {"remove"},
    "pip": {"uninstall"},
    "pip3": {"uninstall"},
    "npm": {"uninstall", "remove", "rm"},
}


class RuleVerdict(BaseModel):
    # None: no rule applies; the call is ambiguous.
    action: PolicyAction | None = None
    risk: str = "low"
    reason: str = ""


AMBIGUOUS = RuleVerdict()
SAFE = RuleVerdict(action=PolicyAction.ALLOW, reason="read-only command")


def needs_approval(reason: str, risk: str = "high") -> RuleVerdict:
    return RuleVerdict(action=PolicyAction.REQUIRE_APPROVAL, risk=risk, reason=reason)


def is_protected_path(path: str) -> bool:
    if path in SAFE_DEVICES:
        return False
    if path.startswith("~"):
        path = HOME + path[1:]
    if not path.startswith("/"):
        path = f"{HOME}/{path}"
    path = posixpath.normpath(path)
    return any(path == root or path.startswith(root + "/") for root in PROTECTED_DIRS)


def tokenize(command: str) -> list[str]:
    # Substitutions and newlines start new commands as far as these rules care.
    prepared = command.replace("$(", " ( ").replace("`", " ; ").replace("\n", " ; ")
    lexer = shlex.shlex(prepared, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: fall back to a crude split so the rules still see the words.
        return prepared.replace(";", " ; ").replace("|", " | ").replace("&&", " && ").split()


def split_segments(tokens: list[str]) -> list[tuple[list[str], str | None]]:
    """Split into simple commands, each with the operator that preceded it."""
    segments: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    operator: str | None = None
    for token in tokens:
        if token in SEPARATORS:
            if current:
                segments.append((current, operator))
            current, operator = [], token
        else:
            current.append(token)
    if current:
        segments.append((current, operator))
    return segments


def unwrap(argv: list[str]) -> list[str]:
    """Strip prefixes such as `sudo`, `env VAR=x` and `nohup` to reach the real command."""
    argv = list(argv)
    while argv:
        head = posixpath.basename(argv[0])
        if "=" in argv[0] and not argv[0].startswith("-") and "/" not in argv[0].split("=")[0]:
            argv = argv[1:]
        elif head in ("sudo", "doas"):
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[2:] if argv[0] in SUDO_OPTIONS_WITH_VALUE else argv[1:]
        elif head == "env" and len(argv) > 1:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
        elif head in WRAPPERS and len(argv) > 1:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
            argv = argv[WRAPPERS[head]:]
        else:
            argv[0] = head
            break
    return argv


def _has_flag(args: list[str], short: str, long: str) -> bool:
    return any(
        arg == long or (arg.startswith("-") and not arg.startswith("--") and short in arg[1:])
        for arg in args
    )


def _redirect_targets(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token in (">", ">>", ">|", "&>")]


def check_command(argv: list[str], operator: str | None, previous: str | None) -> RuleVerdict:
    """Judge one simple command. `previous` is the command feeding it through a pipe."""
    if not argv:
        return AMBIGUOUS
    name, args = argv[0], argv[1:]

    for target in _redirect_targets(argv):
        if is_protected_path(target):
            return needs_approval(f"This overwrites the system path {target}.")
    if name in SHELLS and "-c" in args:
        index = args.index("-c")
        if index + 1 < len(args):
            return classify_shell(args[index + 1])
    if name in SHELLS and operator in ("|", "|&") and previous in DOWNLOADERS:
        return needs_approval("This downloads a script and runs it without review.")
    if name == "rm" and (_has_flag(args, "r", "--recursive") or _has_flag(args, "R", "--recursive")):
        return needs_approval("This deletes files and directories recursively.")
    if name == "rm" and any(is_protected_path(a) for a in args if not a.startswith("-")):
        return needs_approval("This deletes system files.")
    if name == "find" and ("-delete" in args or "-exec" in args or "-execdir" in args):
        return needs_approval("This runs a delete or another command on every file it finds.", "medium")
    if name == "dd" and any(a.startswith("of=/dev/") and a[3:] not in SAFE_DEVICES for a in args):
        return needs_approval("This writes directly to a disk device.")
    if name.startswith("mkfs") or name in DISK_TOOLS:
        return needs_approval("This formats or wipes a disk.")
    if name in POWER_COMMANDS or (name == "init" and args[:1] in (["0"], ["6"])):
        return needs_approval("This shuts down or reboots the computer.", "medium")
    if name == "systemctl" and any(a in ("poweroff", "reboot", "halt", "kexec") for a in args):
        return needs_approval("This shuts down or reboots the computer.", "medium")
    if name in PACKAGE_REMOVALS and any(a in PACKAGE_REMOVALS[name] for a in args):
        return needs_approval("This removes installed software.")
    if name in USER_COMMANDS:
        return needs_approval("This changes system users, groups or passwords.")
    if name in ("chmod", "chown", "chgrp") and _has_flag(args, "R", "--recursive"):
        if any(is_protected_path(a) or a == "/" for a in args if not a.startswith("-")):
            return needs_approval("This changes permissions across system directories.")
    if name in ("tee", "truncate", "mv", "cp", "ln", "install") and any(
        is_protected_path(a) for a in args if not a.startswith("-")
    ):
        return needs_approval("This modifies files in a system directory.", "medium")

    if name in READ_ONLY and not _redirect_targets(argv) and ">" not in "".join(argv):
        return SAFE
    return AMBIGUOUS


def classify_shell(command: str) -> RuleVerdict:
    segments = [(unwrap(argv), operator) for argv, operator in split_segments(tokenize(command))]
    verdicts = []
    previous: str | None = None
    for argv, operator in segments:
        verdict = check_command(argv, operator, previous)
        if verdict.action == PolicyAction.REQUIRE_APPROVAL:
            return verdict
        verdicts.append(verdict)
        previous = argv[0] if argv else None
    if verdicts and all(verdict.action == PolicyAction.ALLOW for verdict in verdicts):
        return SAFE
    return AMBIGUOUS


SQL_STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
SQL_READ_ONLY = ("SELECT", "EXPLAIN", "VALUES")
SQL_CHANGING = ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE", "ATTACH", "VACUUM")


def classify_sql(sql: str) -> RuleVerdict:
    """Judge one SQL statement an agent wants to run on its own database."""
    # Quoted text and comments are removed first, so words inside them are not read as SQL.
    bare = SQL_STRING.sub("''", SQL_COMMENT.sub(" ", sql)).upper()
    words = re.findall(r"[A-Z_]+", bare)
    if not words:
        return AMBIGUOUS
    if "DROP" in words:
        return needs_approval("This permanently removes a table or other object from the database.")
    deletes = any(word == "DELETE" and words[i + 1 : i + 2] == ["FROM"] for i, word in enumerate(words))
    if deletes and "WHERE" not in words:
        return needs_approval("This deletes every row of a table.")
    if "UPDATE" in words and "SET" in words and "WHERE" not in words:
        return needs_approval("This overwrites a value in every row of a table.", "medium")
    changing = any(word in words for word in SQL_CHANGING)
    if not changing and (words[0] in SQL_READ_ONLY or words[0] == "WITH" or words[0] == "PRAGMA" and "=" not in bare):
        return RuleVerdict(action=PolicyAction.ALLOW, reason="read-only query")
    return AMBIGUOUS


def classify(operation: str, arguments: dict[str, Any]) -> RuleVerdict:
    """Apply the hardcoded rules to one proposed tool call."""
    if operation == "db.sql":
        sql = arguments.get("sql")
        return classify_sql(sql) if isinstance(sql, str) else AMBIGUOUS
    if operation == "shell.exec":
        command = arguments.get("command")
        return classify_shell(command) if isinstance(command, str) else AMBIGUOUS
    if operation == "file.write":
        path = arguments.get("path")
        if isinstance(path, str) and is_protected_path(path):
            return needs_approval(f"This writes to the system path {path}.")
        return AMBIGUOUS
    if operation in ("file.read", "file.list", "browser.extract_text", "browser.screenshot",
                     "browser.current_url", "browser.scroll", "browser.search", "channel.read",
                     "channel.post", "memory.remember", "memory.recall", "memory.forget"):
        return RuleVerdict(action=PolicyAction.ALLOW, reason="harmless operation")
    return AMBIGUOUS
