"""Guard against accidental addition of broker capabilities to signal modules."""
import ast
from pathlib import Path
import unittest
from banknifty.offline_runtime import guard_event


class OptionsBoundaryTests(unittest.TestCase):
    def test_options_modules_have_no_broker_or_transport_dependencies(self):
        project = Path(__file__).resolve().parents[1]
        safe_standard = {'__future__','dataclasses','datetime','decimal','hashlib','json','math',
            'typing','collections','pathlib','sqlite3','contextlib','csv','io','tempfile','os',
            'tkinter','threading','queue','enum','statistics','uuid','time','copy','re','html','heapq',
            'argparse','zoneinfo','types','sys','msvcrt','fcntl','gc'}
        pure_modules = {p.stem for p in (project/'banknifty').glob('options_*.py')} - {'options_workspace'}
        pure_modules.update({'market_archive','provider_adapters','research_worker','market_capture'})
        checked = []
        for path in sorted((project/'banknifty'/f'{name}.py') for name in pure_modules | {'options_workspace'}):
            checked.append(path.name)
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or '']
                    for name in names:
                        base = name.split('.')[0]
                        if isinstance(node, ast.ImportFrom) and node.level:
                            self.assertIn(base, pure_modules, (path.name, name))
                        elif name.startswith('banknifty.'):
                            target = name.split('.')[1]
                            # Only the desktop application may load the optional
                            # explanation interface; calculations never call it.
                            allowed = pure_modules | ({'explanation_service'} if path.stem == 'options_workspace' else set())
                            self.assertIn(target, allowed, (path.name, name))
                        else:
                            self.assertIn(base, safe_standard, (path.name, name))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {'eval','exec','__import__'}, path.name)
        self.assertGreaterEqual(len(checked), 6)

    def test_offline_guard_denies_connections_and_credentials(self):
        for event, args in [('socket.connect', (None, ('example.invalid',443))),
                ('socket.getaddrinfo', ('example.invalid',)), ('urllib.Request', ('https://example.invalid',)),
                ('open', ('test.env', 'r', 0)), ('open', ('a.creds','r',0))]:
            # .env exact name is protected; an unrelated test.env isn't a credential convention.
            if args[0] == 'test.env': args = ('.env', 'r', 0)
            with self.subTest(event=event), self.assertRaises(PermissionError): guard_event(event,args)
        guard_event('open', ('quotes.json','r',0))


if __name__ == '__main__': unittest.main()
