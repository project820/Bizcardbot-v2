"""One writer per data directory, shared by the bot and mutating CLI commands.

Lock order: writer flock (process lifetime/CLI command), pipeline card lock,
cards.save_card's in-process write lock. Never unlink the lock file: its inode
must remain stable across crashes. The kernel releases flock on process exit.
"""
from contextlib import contextmanager
import fcntl
from functools import wraps
import os
import sys
import threading
import time

from . import cards

BUSY_MESSAGE = "봇이 실행 중입니다. 텔레그램에서 처리하거나 봇을 멈춘 뒤 다시 실행하세요."
BOT_WAIT_SECONDS = 20.0
_held = threading.local()


@contextmanager
def lock(*, wait_seconds=0.0):
    path = (cards.CARD_DIR.parent / ".writer.lock").resolve()
    owner = (os.getpid(), str(path))
    if getattr(_held, "owner", None) == owner:
        # CLI bot startup -> telegram.run, in the same thread only.
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(.1, remaining))
        _held.owner = owner
        try:
            yield True
        finally:
            _held.owner = None
            fcntl.flock(handle, fcntl.LOCK_UN)


def required(function=None, *, bot_start=False):
    if function is None:
        return lambda fn: required(fn, bot_start=bot_start)
    @wraps(function)
    def wrapped(*args, **kwargs):
        with lock(wait_seconds=BOT_WAIT_SECONDS if bot_start else 0.0) as acquired:
            if not acquired:
                print(BUSY_MESSAGE, file=sys.stderr)
                return 1
            return function(*args, **kwargs)
    return wrapped


def is_held() -> bool:
    """Probe the existing inode without creating files or claiming writer ownership."""
    path = cards.CARD_DIR.parent / ".writer.lock"
    try:
        handle = path.open("r")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False
