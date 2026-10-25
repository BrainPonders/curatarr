"""Run isolated real-component browser journeys with public synthetic fixtures.

No live configuration, Telegram messages or provider writes. Output stays local.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    scripts = sorted(path.stem.removesuffix('_browser_check')
                     for path in (ROOT / 'tests').glob('*_browser_check.py'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', choices=scripts)
    parser.add_argument('--output', type=Path, default=ROOT / '.local/qa/browser')
    args = parser.parse_args()
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT/'src'), str(ROOT/'tests')]),
                       PYTHONDONTWRITEBYTECODE='1')
    temporary = ROOT / '.local' / 'test-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    environment['TMPDIR'] = str(temporary)
    for name in args.only or scripts:
        script = ROOT / 'tests' / f'{name}_browser_check.py'
        command = [sys.executable, str(script), '--output', str(args.output.resolve()/name)]
        if re.search(r'add_argument\([\"\x27]--poster[\"\x27]', script.read_text()):
            command += ['--poster', str(ROOT/'tests/fixtures/poster.png')]
        print(f'Browser journey: {name}', flush=True)
        subprocess.run(command, cwd=ROOT, env=environment, check=True, timeout=300)


if __name__ == '__main__':
    main()
