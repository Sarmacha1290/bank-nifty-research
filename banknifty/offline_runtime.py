"""Offline desktop session guard, installed only by the desktop entrypoint.

This is defense in depth for accidental application calls, not an OS sandbox
against malicious code. Pure options modules have no transport dependency.
"""
import os
from pathlib import Path
import sys

_installed = False


def guard_event(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                 'socket.gethostbyaddr', 'urllib.Request', 'http.client.connect'}:
        raise PermissionError('Offline research mode: network connections are disabled')
    if event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0]))
        if path.suffix.lower() == '.creds' or path.name.lower() == '.env':
            raise PermissionError('Offline research mode: credential files are disabled')


def install_offline_guard():
    global _installed
    if not _installed:
        sys.addaudithook(guard_event)
        _installed = True
