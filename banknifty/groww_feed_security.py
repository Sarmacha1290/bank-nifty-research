"""Contain Groww SDK 1.5.0's socket credential files for one feed session.

That SDK writes a socket JWT and signing seed as a.creds and b.creds beside
its installed common/files.py module. Redirect those writes to a restricted,
temporary directory before any socket connection is constructed. This is a
version-specific safeguard, not a claim about future SDK implementations.
"""

from contextlib import contextmanager
import asyncio
import csv
from hashlib import sha256
from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Lock


REVIEWED_GROWW_SDK_VERSION = "1.5.0"
REVIEWED_SDK_FILE_HASHES = {
    "files.py": "78620e632baafa7458c4a8e263be4b4d90c6dbbdb0453a98a9be39df858e9c2b",
    "feed.py": "0fb4dbdac52e5abb13193a27c90d06379c6a62274f68c041a7aeff9eeb867919",
    "nats_client.py": "16856790552f7e06a489f1d0cc40e36c31d4f24a61513a510bec9fddb20a457f",
}
_construction_lock = Lock()
_SOCKET_SHUTDOWN_SECONDS = 10


class GrowwFeedSecurityError(RuntimeError):
    """The local SDK credential safeguard could not be established."""


class GrowwFeedSetupError(RuntimeError):
    """Fixed-label SDK construction failure; never includes an upstream error."""

    def __init__(self, stage):
        if stage not in ("before_socket_credentials", "during_socket_credentials",
                         "after_socket_credentials"):
            raise ValueError("Invalid feed setup stage")
        self.stage = stage
        super().__init__(stage.replace("_", " "))


def _current_windows_sid():
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, check=True)
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
        raise GrowwFeedSecurityError("Could not identify the Windows user for feed credential isolation")
    return rows[0][1]


@contextmanager
def _restricted_socket_directory():
    if os.name != "nt":
        raise GrowwFeedSecurityError("Groww socket credential isolation is reviewed only on Windows")
    parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="banknifty-groww-socket-") as folder:
        directory = Path(folder).resolve()
        if directory.parent != parent:
            raise GrowwFeedSecurityError("Socket credential directory escaped the Windows temp folder")
        sid = _current_windows_sid()
        result = subprocess.run(["icacls", str(directory), "/inheritance:r",
                                 "/grant:r", f"*{sid}:(OI)(CI)F"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise GrowwFeedSecurityError("Could not restrict Groww socket credential directory")
        try:
            yield directory
        finally:
            if directory.resolve().parent != parent:
                raise GrowwFeedSecurityError("Socket credential directory moved before cleanup")


def verify_reviewed_sdk_source():
    """Check the exact socket source reviewed for this safeguard, offline."""
    if os.name != "nt":
        raise GrowwFeedSecurityError("Groww socket credential isolation is reviewed only on Windows")
    if version("growwapi") != REVIEWED_GROWW_SDK_VERSION:
        raise GrowwFeedSecurityError("Groww SDK version changed; review socket credential handling before capture")
    from growwapi.common import files as file_utils
    from growwapi.groww import feed as feed_module, nats_client

    for name, module in (("files.py", file_utils), ("feed.py", feed_module),
                         ("nats_client.py", nats_client)):
        try:
            digest = sha256(Path(module.__file__).read_bytes()).hexdigest()
        except OSError as exc:
            raise GrowwFeedSecurityError("Groww SDK source could not be checked") from exc
        if digest != REVIEWED_SDK_FILE_HASHES[name]:
            raise GrowwFeedSecurityError("Groww SDK socket implementation changed; review before capture")


def _close_reviewed_feed(feed):
    """Close SDK 1.5.0's cached NATS client before deleting its credential files."""
    from growwapi import GrowwFeed

    client = getattr(feed, "_nats_client", None)
    key = getattr(feed, "_client_key", None)
    if client is None or key is None:
        return  # A local constructor stub may not create a socket.
    loop = client._loop
    thread = client.consume_thread
    try:
        if loop.is_running():
            if client._socket is not None:
                future = asyncio.run_coroutine_threadsafe(client._socket.close(), loop)
                future.result(timeout=_SOCKET_SHUTDOWN_SECONDS)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=_SOCKET_SHUTDOWN_SECONDS)
            if thread.is_alive():
                raise GrowwFeedSecurityError("Groww socket thread did not stop")
        elif client._socket is not None and not loop.is_closed():
            loop.run_until_complete(client._socket.close())
    except Exception as exc:
        raise GrowwFeedSecurityError("Could not confirm Groww socket shutdown") from exc
    finally:
        # SDK 1.5.0 otherwise retains JWT and signing seed in this class cache.
        GrowwFeed._nats_clients.pop(key, None)
        if not loop.is_running() and not loop.is_closed():
            loop.close()
        client.nats_token = None
        client.nkey_seed = None
        client.callback = None
        feed._nats_client = None
        feed._client_key = None


@contextmanager
def isolated_groww_feed(api):
    """Construct the reviewed SDK feed without leaving socket keys in site-packages.

    Keep the private files until capture exits in case the SDK reconnects. The
    temporary directory is removed on normal completion or an exception.
    Forced process termination or power loss can still leave files behind;
    never print their contents or include them in project backups.
    """
    verify_reviewed_sdk_source()
    from growwapi import GrowwFeed
    from growwapi.common import files as file_utils

    with _restricted_socket_directory() as directory:
        with _construction_lock:
            original_get_cwd = file_utils.get_cwd
            try:
                file_utils.get_cwd = lambda file=None: str(directory)
                try:
                    feed = GrowwFeed(api)
                except Exception:
                    present = {item.name for item in directory.iterdir()}
                    stage = ("before_socket_credentials" if not present else
                             "after_socket_credentials" if present == {"a.creds", "b.creds"}
                             else "during_socket_credentials")
                    raise GrowwFeedSetupError(stage) from None
            finally:
                file_utils.get_cwd = original_get_cwd
        try:
            children = list(directory.iterdir())
            if ({item.name for item in children} != {"a.creds", "b.creds"} or any(
                    not item.is_file() or item.is_symlink() or item.stat().st_size == 0
                    for item in children)):
                raise GrowwFeedSecurityError("Groww SDK socket credential output did not match the reviewed layout")
            yield feed
        finally:
            _close_reviewed_feed(feed)
