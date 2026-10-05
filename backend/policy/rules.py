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
    "printenv", "stat", "file", "find", "lsb_release", "true", "false", "test", "[", "cd",
    "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath", "readlink", "nproc", "lscpu",
    "lsblk", "ss", "diff", "md5sum", "sha256sum", "jq", "column",
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

    def not_safe(self) -> RuleVerdict:
        """This verdict, except that "read-only" becomes "no rule applies"."""
        return self if self.action != PolicyAction.ALLOW else RuleVerdict()


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


OPERATORS = ("&&", "||", "|&", ">>", ">|", "&>", ";", "|", "&", "(", ")", "{", "}", "<", ">")
REDIRECTS = {">", ">>", ">|", "&>", "<"}
# Ways a shell runs one command inside another. Their contents are not visible
# as separate commands to a tokenizer when they sit inside double quotes.
SUBSTITUTION_MARKS = ("$(", "`", "<(", ">(")
# Options of read-only tools that make them write a file or run something.
WRITING_OPTIONS = {
    "find": ("-fprint", "-fprint0", "-fprintf", "-fls", "-ok", "-okdir"),
    "sort": ("-o", "--output"),
}
SUDO_LONG_OPTIONS_WITH_VALUE = {
    "--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type", "--other-user",
    "--close-from", "--command-timeout",
}
TIMEOUT_OPTIONS_WITH_VALUE = {"-s", "--signal", "-k", "--kill-after"}


def split_operators(token: str) -> list[str]:
    """Split a run of shell punctuation such as ';(' or ');' into single operators."""
    parts, rest = [], token
    while rest:
        operator = next((op for op in OPERATORS if rest.startswith(op)), rest[0])
        parts.append(operator)
        rest = rest[len(operator):]
    return parts


def tokenize(command: str) -> list[str]:
    # Substitutions and newlines start new commands as far as these rules care.
    prepared = command.replace("$(", " ( ").replace("`", " ; ").replace("\n", " ; ")
    lexer = shlex.shlex(prepared, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        raw = list(lexer)
    except ValueError:
        # Unbalanced quotes: fall back to a crude split so the rules still see the words.
        raw = prepared.replace(";", " ; ").replace("|", " | ").replace("&&", " && ").split()
    tokens: list[str] = []
    for token in raw:
        if token and all(char in "();<>|&{}" for char in token):
            tokens += split_operators(token)  # the lexer glues neighbouring operators together
        else:
            tokens.append(token)
    return tokens


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


def without_redirects(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate a command's words from its redirections. Returns (words, files written to)."""
    words: list[str] = []
    written: list[str] = []
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
        elif token in REDIRECTS:
            if token != "<" and index + 1 < len(argv):
                written.append(argv[index + 1])
            skip = True
        else:
            words.append(token)
    return words, written


def unwrap(argv: list[str]) -> tuple[list[str], bool]:
    """Strip prefixes such as `sudo`, `env VAR=x` and `nohup` to reach the real command.

    Also reports whether variables were set for the command (`LD_PRELOAD=x ls`):
    that can change what even a harmless command does.
    """
    argv = list(argv)
    assigned = False
    while argv:
        head = posixpath.basename(argv[0])
        if "=" in argv[0] and not argv[0].startswith("-") and "/" not in argv[0].split("=")[0]:
            argv, assigned = argv[1:], True
        elif head in ("sudo", "doas"):
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                takes_value = argv[0] in SUDO_OPTIONS_WITH_VALUE or argv[0] in SUDO_LONG_OPTIONS_WITH_VALUE
                argv = argv[2:] if takes_value else argv[1:]
        elif head == "env" and len(argv) > 1:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
        elif head == "timeout" and len(argv) > 1:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[2:] if argv[0] in TIMEOUT_OPTIONS_WITH_VALUE else argv[1:]
            argv = argv[1:]  # the duration
        elif head in WRAPPERS and len(argv) > 1:
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
        else:
            argv[0] = head
            break
    return argv, assigned


def _has_flag(args: list[str], short: str, long: str) -> bool:
    return any(
        arg == long or (arg.startswith("-") and not arg.startswith("--") and short in arg[1:])
        for arg in args
    )


def _recursive_rm(words: list[str]) -> bool:
    """`rm` with a recursive flag anywhere among the words, whatever came before it.

    A net under the prefix-stripping above: a wrapper spelled in a way it does
    not know must not hide a recursive delete.
    """
    for index, word in enumerate(words):
        if posixpath.basename(word) == "rm":
            rest = words[index + 1 :]
            if _has_flag(rest, "r", "--recursive") or _has_flag(rest, "R", "--recursive"):
                return True
    return False


def check_command(argv: list[str], operator: str | None, previous: str | None) -> RuleVerdict:
    """Judge one simple command. `previous` is the command feeding it through a pipe."""
    words, written = without_redirects(argv)
    written = [target for target in written if target not in SAFE_DEVICES]  # 2>/dev/null writes nothing
    words, assigned = unwrap(words)
    if not words:
        return AMBIGUOUS
    name, args = words[0], words[1:]

    for target in written:
        if is_protected_path(target):
            return needs_approval(f"This overwrites the system path {target}.")
    if name in SHELLS:
        # -c, and clusters such as -lc, take a command line to run.
        for index, arg in enumerate(args):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:] and index + 1 < len(args):
                return classify_shell(args[index + 1]).not_safe()
    if name == "eval":
        return classify_shell(" ".join(args)).not_safe()
    if name in SHELLS and operator in ("|", "|&") and previous in DOWNLOADERS:
        return needs_approval("This downloads a script and runs it without review.")
    if _recursive_rm(words):
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

    # Read-only tools have options that write: `find -fprint FILE`, `sort -o FILE`.
    writes = any(
        arg == option or arg.startswith(option + "=") for option in WRITING_OPTIONS.get(name, ()) for arg in args
    )
    if writes:
        targets = [a.split("=", 1)[1] if a.startswith("--") and "=" in a else a for a in args]
        if any(is_protected_path(t) for t in targets if not t.startswith("-")):
            return needs_approval("This overwrites a file in a system directory.")
        return AMBIGUOUS
    if name == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return AMBIGUOUS  # the second file name is an output file

    if name in READ_ONLY and not written and not assigned and ">" not in "".join(words):
        return SAFE
    return AMBIGUOUS


def classify_shell(command: str) -> RuleVerdict:
    verdict = _classify_segments(command)
    if verdict.action == PolicyAction.REQUIRE_APPROVAL:
        return verdict
    if any(mark in command for mark in SUBSTITUTION_MARKS):
        # A command run inside another ("$(...)", backticks, <(...)) can hide inside
        # quotes. Look again with the quotes removed, and never call the line read-only.
        hidden = _classify_segments(command.replace('"', " ").replace("'", " "))
        if hidden.action == PolicyAction.REQUIRE_APPROVAL:
            return hidden
        return AMBIGUOUS
    return verdict


def _classify_segments(command: str) -> RuleVerdict:
    verdicts = []
    previous: str | None = None
    for argv, operator in split_segments(tokenize(command)):
        verdict = check_command(argv, operator, previous)
        if verdict.action == PolicyAction.REQUIRE_APPROVAL:
            return verdict
        verdicts.append(verdict)
        words, _ = unwrap(without_redirects(argv)[0])
        previous = words[0] if words else None
    if verdicts and all(verdict.action == PolicyAction.ALLOW for verdict in verdicts):
        return SAFE
    return AMBIGUOUS


# Quoted text and comments, matched in one left-to-right pass: a comment marker
# inside a string is text, and a quote inside a comment is not the start of a string.
SQL_QUOTED_OR_COMMENT = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?(?:\*/|$)""", re.DOTALL
)
SQL_READ_ONLY = ("SELECT", "EXPLAIN", "VALUES")
SQL_CHANGING = ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE", "ATTACH", "VACUUM")
# A WHERE clause that selects every row is no restriction at all.
SQL_ALWAYS_TRUE = re.compile(r"\bWHERE\s+(?:1|TRUE|1\s*=\s*1|''\s*=\s*'')\s*(?:;|$)")


def classify_sql(sql: str) -> RuleVerdict:
    """Judge one SQL statement an agent wants to run on its own database."""
    bare = SQL_QUOTED_OR_COMMENT.sub(lambda m: "''" if m.group()[0] in "'\"" else " ", sql).upper().strip()
    words = re.findall(r"[A-Z_]+", bare)
    if not words:
        return AMBIGUOUS
    restricted = "WHERE" in words and not SQL_ALWAYS_TRUE.search(bare)
    if "DROP" in words:
        return needs_approval("This permanently removes a table or other object from the database.")
    deletes = any(word == "DELETE" and words[i + 1 : i + 2] == ["FROM"] for i, word in enumerate(words))
    if deletes and not restricted:
        return needs_approval("This deletes every row of a table.")
    if "UPDATE" in words and "SET" in words and not restricted:
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
