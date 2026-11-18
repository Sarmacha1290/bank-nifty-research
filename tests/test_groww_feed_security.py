"""The reviewed SDK must write socket credentials outside its installed code."""

import asyncio
from importlib.util import find_spec
from pathlib import Path
import os
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from banknifty.groww_feed_security import (GrowwFeedSecurityError,
                                          GrowwFeedSetupError,
                                          _close_reviewed_feed,
                                          isolated_groww_feed)


@unittest.skipUnless(os.name == "nt", "Windows socket credential isolation only")
class GrowwFeedSecurityTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_reviewed_socket_client_is_closed_and_evicted(self):
        from growwapi import GrowwFeed

        class Socket:
            closed = False

            async def close(self):
                self.closed = True

        loop = asyncio.new_event_loop()
        started = threading.Event()

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        worker = threading.Thread(target=run_loop, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        socket = Socket()
        key = ("dummy-jwt", "dummy-seed")
        client = SimpleNamespace(_loop=loop, consume_thread=worker, _socket=socket,
                                 nats_token=key[0], nkey_seed=key[1], callback=lambda: None)
        feed = SimpleNamespace(_nats_client=client, _client_key=key)
        GrowwFeed._nats_clients[key] = client
        try:
            _close_reviewed_feed(feed)
            self.assertTrue(socket.closed)
            self.assertFalse(worker.is_alive())
            self.assertTrue(loop.is_closed())
            self.assertNotIn(key, GrowwFeed._nats_clients)
            self.assertIsNone(client.nats_token)
            self.assertIsNone(client.nkey_seed)
            self.assertIsNone(feed._client_key)
        finally:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            worker.join(2)
            if not loop.is_closed():
                loop.close()
            GrowwFeed._nats_clients.pop(key, None)

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_invalid_credential_layout_closes_constructed_feed(self):
        import growwapi
        from growwapi.common import files as file_utils

        for layout in ("missing_seed", "empty_seed", "directory_seed", "extra_file"):
            with self.subTest(layout=layout):
                loop = asyncio.new_event_loop()
                captured = []
                constructed = []
                original_get_cwd = file_utils.get_cwd

                class Socket:
                    close_calls = 0
                    directory_present_at_close = False

                    async def close(self):
                        self.close_calls += 1
                        self.directory_present_at_close = captured[0].parent.is_dir()

                socket = Socket()
                key = ("dummy-layout-jwt", "dummy-layout-seed")
                client = SimpleNamespace(_loop=loop, consume_thread=None, _socket=socket,
                                         nats_token=key[0], nkey_seed=key[1], callback=None)

                class InvalidLayoutFeed:
                    _nats_clients = {}

                    def __init__(self, api):
                        token_file = Path(file_utils.generate_token_file("dummy-socket-jwt"))
                        captured.append(token_file)
                        seed_file = token_file.parent / "b.creds"
                        if layout == "empty_seed":
                            seed_file.write_bytes(b"")
                        elif layout == "directory_seed":
                            seed_file.mkdir()
                        elif layout == "extra_file":
                            file_utils.generate_seed_file("dummy-signing-seed")
                            (token_file.parent / "unexpected.txt").write_text("dummy")
                        self._nats_client = client
                        self._client_key = key
                        self._nats_clients[key] = client
                        constructed.append(self)

                try:
                    with patch.object(growwapi, "GrowwFeed", InvalidLayoutFeed):
                        with self.assertRaisesRegex(GrowwFeedSecurityError, "reviewed layout"):
                            with isolated_groww_feed(object()):
                                self.fail("Invalid layout must not yield a feed")
                    self.assertEqual(len(constructed), 1)
                    self.assertEqual(socket.close_calls, 1)
                    self.assertTrue(socket.directory_present_at_close)
                    self.assertTrue(loop.is_closed())
                    self.assertNotIn(key, InvalidLayoutFeed._nats_clients)
                    self.assertIsNone(constructed[0]._nats_client)
                    self.assertIsNone(constructed[0]._client_key)
                    self.assertIsNone(client.nats_token)
                    self.assertIsNone(client.nkey_seed)
                    self.assertIs(file_utils.get_cwd, original_get_cwd)
                    self.assertFalse(captured[0].parent.exists())
                finally:
                    if not loop.is_closed():
                        loop.close()
                    InvalidLayoutFeed._nats_clients.pop(key, None)

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_temporary_user_only_socket_credentials_are_removed(self):
        import growwapi
        from growwapi.common import files as file_utils

        installed_folder = Path(file_utils.__file__).resolve().parent
        package_credential_names_before = {
            name for name in ("a.creds", "b.creds")
            if (installed_folder / name).exists()
        }
        captured = []

        class FakeFeed:
            def __init__(self, api):
                captured.append(Path(file_utils.generate_token_file("dummy-socket-jwt")))
                captured.append(Path(file_utils.generate_seed_file("dummy-signing-seed")))

        original_get_cwd = file_utils.get_cwd
        with patch.object(growwapi, "GrowwFeed", FakeFeed):
            with isolated_groww_feed(object()) as feed:
                self.assertIsInstance(feed, FakeFeed)
                self.assertEqual({item.name for item in captured}, {"a.creds", "b.creds"})
                self.assertEqual(captured[0].parent, captured[1].parent)
                self.assertNotEqual(captured[0].parent, installed_folder)
                self.assertTrue(all(item.exists() for item in captured))
                acl = subprocess.run(["icacls", str(captured[0].parent)],
                                     capture_output=True, text=True, check=True)
                self.assertNotIn("(I)", acl.stdout)
        self.assertIs(file_utils.get_cwd, original_get_cwd)
        self.assertTrue(all(not item.exists() for item in captured))
        self.assertEqual(package_credential_names_before, {
            name for name in ("a.creds", "b.creds")
            if (installed_folder / name).exists()
        })

    def test_unreviewed_sdk_version_fails_before_socket_construction(self):
        with patch("banknifty.groww_feed_security.version", return_value="new-version"):
            with self.assertRaisesRegex(GrowwFeedSecurityError, "version changed"):
                with isolated_groww_feed(object()):
                    self.fail("An unreviewed SDK must not start")

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_modified_sdk_socket_source_fails_before_socket_construction(self):
        from banknifty import groww_feed_security as guard
        with patch.dict(guard.REVIEWED_SDK_FILE_HASHES, {"files.py": "0" * 64}):
            with self.assertRaisesRegex(GrowwFeedSecurityError, "implementation changed"):
                with isolated_groww_feed(object()):
                    self.fail("Modified SDK source must not start")

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_constructor_failure_still_removes_private_credentials(self):
        import growwapi
        from growwapi.common import files as file_utils
        captured = []

        class FailingFeed:
            def __init__(self, api):
                captured.append(Path(file_utils.generate_token_file("dummy-socket-jwt")))
                raise ValueError("simulated socket failure")

        with patch.object(growwapi, "GrowwFeed", FailingFeed):
            with self.assertRaises(GrowwFeedSetupError) as caught:
                with isolated_groww_feed(object()):
                    self.fail("Constructor failure must stop capture")
        self.assertEqual(caught.exception.stage, "during_socket_credentials")
        self.assertNotIn("simulated socket failure", str(caught.exception))
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_constructor_failure_before_credential_files_has_safe_stage(self):
        import growwapi

        class FailingFeed:
            def __init__(self, api):
                raise RuntimeError("SECRET_TEST_TOKEN")

        with patch.object(growwapi, "GrowwFeed", FailingFeed):
            with self.assertRaises(GrowwFeedSetupError) as caught:
                with isolated_groww_feed(object()):
                    self.fail("Feed must not begin")
        self.assertEqual(caught.exception.stage, "before_socket_credentials")
        self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))

    @unittest.skipUnless(find_spec("growwapi") is not None, "Optional Groww SDK is not installed")
    def test_constructor_failure_after_credential_files_has_safe_stage(self):
        import growwapi
        from growwapi.common import files as file_utils

        class FailingFeed:
            def __init__(self, api):
                file_utils.generate_token_file("dummy-socket-jwt")
                file_utils.generate_seed_file("dummy-signing-seed")
                raise RuntimeError("SECRET_TEST_TOKEN")

        with patch.object(growwapi, "GrowwFeed", FailingFeed):
            with self.assertRaises(GrowwFeedSetupError) as caught:
                with isolated_groww_feed(object()):
                    self.fail("Feed must not begin")
        self.assertEqual(caught.exception.stage, "after_socket_credentials")
        self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
