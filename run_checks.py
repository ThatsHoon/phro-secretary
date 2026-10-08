"""Offline audit checks. Run with the same Python used by the application."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    commands = []
    with tempfile.TemporaryDirectory(prefix='phro-checks-') as tmp:
        commands += [[sys.executable, '-m', 'pytest', '-q', 'tests', '--basetemp=' + str(Path(tmp) / 'pytest'),
                      '-p', 'no:cacheprovider', '--tb=short'],
                     ['node', '--test', *[str(p) for p in sorted((root / 'desktop').glob('test-*.mjs'))]]]
        if '--e2e' in sys.argv:
            # Opens real windows on Windows; stand-in Claude, synthetic DB, isolated profile.
            commands.append(['node', str(root / 'tests' / 'e2e_desktop.mjs')])
        failures = []
        for command in commands:
            result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace')
            label = ' '.join(command[:3])
            print(('PASS ' if result.returncode == 0 else 'FAIL ') + label, flush=True)
            if result.returncode == 0:
                if command[0] == 'node':
                    print('\n'.join(line for line in result.stdout.splitlines() if line.startswith(('# tests ', '# pass ', '# fail ')) or 'E2E checks' in line))
                else:
                    print(result.stdout.strip().splitlines()[-1])
            if result.returncode:
                print(result.stdout + result.stderr)
                failures.append(label)
        print(f'{len(commands)-len(failures)}/{len(commands)} check groups passed')
        return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
