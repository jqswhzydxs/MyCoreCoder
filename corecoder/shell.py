"""shell.py
Shell routing: POSIX commands need a POSIX shell.

On macOS/Linux `shell=True` already means /bin/sh. On Windows it means
cmd.exe, where pwd, ls, cat, sleep and friends do not exist. Every shell
spawn in CoreCoder goes through here so Windows gets Git Bash instead.
"""

import os
import re
import shutil
import subprocess
from functools import lru_cache

# Drive-letter span: `C:\Users\...` up to a shell metacharacter. Escapes like
# `\n` never follow a drive letter, so they pass through untouched.
_DRIVE_PATH_RE = re.compile(r"([A-Za-z]):((?:\\[^\s;&|<>()'\"]+)+)")


def _posixify_drive_paths(command: str) -> str:
    """Rewrite drive-letter paths to git-bash's ``C:/...`` form.

    bash's lexer eats backslashes, so an unquoted ``C:\\Users\\runner`` hook
    path or ``cd`` target comes out as ``C:Usersrunner`` and the command
    fails silently. git-bash accepts the forward-slash form natively, and no
    shell escape sequence looks like a drive letter.
    """
    return _DRIVE_PATH_RE.sub(lambda m: m.group(1) + ":" + m.group(2).replace("\\", "/"), command)


@lru_cache(maxsize=1)
def _git_bash() -> str | None:
    """Locate Git Bash on Windows; None elsewhere or when absent.

    System32\\bash.exe is WSL (a different filesystem and environment, not
    the Git installation the user's commands target), so it is never picked.
    """
    if os.name != "nt":
        return None
    candidate = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    git = shutil.which("git")
    if git:
        derived = os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")
        if os.path.isfile(derived):
            return derived
    return None


def run_shell(command: str, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Run one POSIX command string, via Git Bash on Windows when present.

    The list form bypasses cmd.exe entirely; without Git Bash we fall back
    to the platform default (cmd.exe on Windows, /bin/sh elsewhere).
    """
    bash = _git_bash()
    if bash is not None:
        return subprocess.run([bash, "-c", _posixify_drive_paths(command)], shell=False, check=check, **kwargs)
    return subprocess.run(command, shell=True, check=check, **kwargs)


def spawn_shell(command: str, **kwargs) -> subprocess.Popen:
    """Spawn one POSIX command string with the same routing as ``run_shell``.

    The child is placed in its own process group/session so a cancelled tool
    call can terminate the whole tree, not merely the shell that fronts it.
    Callers own stdout/stderr pipes and must reap the process.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP is the portable Windows way to keep a
        # child (and its descendants) distinct from the Agent's console.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if flags:
            kwargs.setdefault("creationflags", flags)
    else:
        kwargs.setdefault("start_new_session", True)

    bash = _git_bash()
    if bash is not None:
        return subprocess.Popen(
            [bash, "-c", _posixify_drive_paths(command)],
            shell=False,
            **kwargs,
        )
    return subprocess.Popen(command, shell=True, **kwargs)
