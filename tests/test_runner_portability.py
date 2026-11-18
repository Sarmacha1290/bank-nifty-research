"""Exercise the exact Windows discard-device/version fallback from review."""
import os
from pathlib import Path
import platform
import unittest
from run_tests import is_null_device, is_system_version_probe


class RunnerPortabilityTests(unittest.TestCase):
    def test_os_discard_device_can_open_read_write_under_guard(self):
        self.assertTrue(is_null_device(os.devnull))
        handle = os.open(os.devnull, os.O_RDWR)
        try:
            self.assertEqual(os.write(handle,b'offline portability fixture'),27)
        finally:
            os.close(handle)
        for value in ['nul.txt','not-nul',r'C:\private\nul.txt',r'\\.\PhysicalDrive0']:
            self.assertFalse(is_null_device(value))

    @unittest.skipUnless(os.name=='nt','Windows platform fallback only')
    def test_only_exact_system_cmd_version_probe_is_allowed(self):
        cmd = str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/cmd.exe')
        self.assertTrue(is_system_version_probe(cmd,f'"{cmd}" /c "ver"'))
        for suffix in ['/c "ver & echo private"','/c "type secret"','/c "ver > file"','/k ver','/c version']:
            self.assertFalse(is_system_version_probe(cmd,f'"{cmd}" {suffix}'))
        self.assertFalse(is_system_version_probe(r'C:\fake\cmd.exe',r'"C:\fake\cmd.exe" /c ver'))

    @unittest.skipUnless(os.name=='nt','Windows platform fallback only')
    def test_real_stdlib_version_fallback_runs_without_broker_or_network(self):
        system,release,version = platform._syscmd_ver()
        self.assertTrue(version)
        self.assertTrue(system)


if __name__=='__main__': unittest.main()
