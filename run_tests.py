"""Run offline tests in a disposable source copy, never the working data folder."""
from pathlib import Path
import ast
from contextlib import contextmanager
import gc
import json
import os
import re
import shutil
import site
import sys
import tempfile
import threading
import unittest


def collect_test_cycles():
    """Finalize discarded Tk fixtures on the thread that created their Tcl state."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Test fixture collection must run on the main thread')
    return gc.collect()


@contextmanager
def main_thread_test_runtime():
    """Control test-fixture GC without suppressing worker or finalizer errors.

    Tk tests destroy their roots but can leave cyclic Python fixture references.
    Automatic cyclic GC would otherwise finalize them on whichever later worker
    happens to allocate objects. This policy belongs to the offline test runner;
    it does not repair or intercept production code's cross-thread UI access.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('The offline test runner must run on the main thread')
    automatic_gc = gc.isenabled()
    entry_threads = set(threading.enumerate())
    old_unraisable = sys.unraisablehook
    old_thread_hook = threading.excepthook
    issues = []

    def unraisable(args):
        issues.append({'kind': 'unraisable', 'exception_type': getattr(args.exc_type, '__name__', 'unknown')})
        old_unraisable(args)

    def worker_exception(args):
        issues.append({'kind': 'worker_exception', 'exception_type': getattr(args.exc_type, '__name__', 'unknown')})
        old_thread_hook(args)

    sys.unraisablehook = unraisable
    threading.excepthook = worker_exception
    gc.disable()
    try:
        collect_test_cycles()
        yield issues
    finally:
        try:
            collect_test_cycles()
        finally:
            unfinished = sum(thread not in entry_threads and thread.is_alive()
                             for thread in threading.enumerate())
            if unfinished:
                issues.append({'kind': 'unfinished_workers', 'count': unfinished})
            sys.unraisablehook = old_unraisable
            threading.excepthook = old_thread_hook
            if automatic_gc:
                gc.enable()
            else:
                gc.disable()


class MainThreadCleanupResult(unittest.TextTestResult):
    def startTest(self, test):
        collect_test_cycles()
        super().startTest(test)

    def stopTest(self, test):
        try:
            super().stopTest(test)
        finally:
            collect_test_cycles()


def is_null_device(path):
    """Only the OS discard device, not similarly named filesystem targets."""
    value = os.fsdecode(path)
    if os.name == 'nt':
        return value.casefold() in {'nul', r'\\.\nul'}
    return value == '/dev/null'


def is_system_version_probe(executable, command):
    """Permit Python platform's exact Windows `cmd /c ver` fallback only."""
    if os.name != 'nt' or not isinstance(command, str):
        return False
    match = re.fullmatch(r'\s*(?:"([^"\r\n]+)"|([^\s"\r\n]+))\s+/c\s+"?ver"?\s*', command, re.I)
    if not match:
        return False
    selected = match.group(1) or match.group(2)
    actual = Path(executable or selected).resolve()
    expected = (Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/cmd.exe').resolve()
    return actual == expected and Path(selected).resolve() == expected


def main():
    source = Path(__file__).resolve().parent
    tree = ast.parse((source/'banknifty/project_backup.py').read_text(encoding='utf-8'))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'ROOT_FILES','CODE_FILES'} for t in node.targets):
            names.update(ast.literal_eval(node.value.args[0]))
    names.update({'tests/__init__.py', 'tests/fixtures/synthetic-bank-weights.json'})
    # Keep the evidence directory so failures remain reviewable.
    # Keep nested fixture/hash paths below legacy Windows path limits.
    evidence = Path(tempfile.mkdtemp(prefix='bn-')).resolve()
    project = evidence/'project'; scratch = evidence/'fixtures'
    project.mkdir(); scratch.mkdir()
    for name in sorted(names):
        origin = source/name
        if origin.is_file() and not origin.is_symlink():
            destination = project/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
    tempfile.tempdir = str(scratch)
    os.environ['TEMP'] = os.environ['TMP'] = str(scratch)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(project))
    sys.path.append(site.getusersitepackages())
    os.chdir(project)
    blocked = []
    def within(path, root):
        try:
            return Path(path).resolve().is_relative_to(root)
        except (TypeError, ValueError, OSError):
            return False
    def deny(reason):
        blocked.append(reason)
        raise RuntimeError('Offline test guard: '+reason)
    def guard(event, args):
        if event in {'http.client.connect','urllib.Request'}:
            deny('HTTP disabled')
        if event in {'socket.connect','socket.getaddrinfo','socket.gethostbyname','socket.gethostbyaddr'}:
            address = args[1] if event == 'socket.connect' else args[0]
            host = address[0] if isinstance(address, tuple) else address
            if host not in {'127.0.0.1','::1','localhost'}:
                deny('External socket/DNS disabled')
        if event == 'subprocess.Popen':
            command = args[0] or args[1]
            executable = Path(command.split(' ',1)[0].strip('"')).name.lower() if isinstance(command,str) else Path(command[0]).name.lower()
            if executable not in {'whoami','whoami.exe','icacls','icacls.exe'} and not is_system_version_probe(args[0], args[1]):
                deny('Unreviewed subprocess disabled')
        if event == 'open' and isinstance(args[0],(str,bytes,os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if (path.suffix.lower()=='.creds' or path.name.lower()=='.env') and not within(path,scratch):
                deny('Credential files outside synthetic fixtures disabled')
            flags = args[2] if len(args)>2 and isinstance(args[2],int) else 0
            if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND) and not within(path,evidence) and not is_null_device(args[0]):
                deny('Writes outside test evidence disabled')
    sys.addaudithook(guard)
    with (evidence/'tests.log').open('w',encoding='utf-8') as output, main_thread_test_runtime() as runtime_errors:
        suite = unittest.defaultTestLoader.discover(str(project/'tests'),pattern='test_*.py')
        result = unittest.TextTestRunner(stream=output,verbosity=2,
            resultclass=MainThreadCleanupResult).run(suite)
    summary = {'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'skipped':[str(test)+': '+reason for test,reason in result.skipped],
        'guard_blocks':blocked,'runtime_errors':runtime_errors,
        'successful':result.wasSuccessful() and not blocked and not runtime_errors}
    (evidence/'results.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary)); print('Evidence:',evidence)
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
