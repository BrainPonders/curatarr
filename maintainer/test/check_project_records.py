"""Check current planning records and portable browser prerequisites without providers.

Run from any directory with Python 3.11+. Product meaning still needs human review.
"""
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]


def rows(text, prefix):
    return [line for line in text.splitlines() if re.match(r'\| ' + prefix + r'\d+\b', line)]


def heading_ids(text):
    result = set()
    for heading in re.findall(r'^#{1,6} (.+)$', text, re.M):
        heading = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', heading).strip().lower()
        result.add(re.sub(r'[^\w\- ]', '', heading).replace(' ', '-'))
    return result


def check(root=ROOT):
    errors = []
    owners = {
        'F': 'maintainer/FEATURES.md', 'W': 'maintainer/ROADMAP.md',
        'DATA': 'maintainer/ROADMAP.md', 'U': 'maintainer/WORKFLOW.md',
        'D': 'documentation/Decisions.md', 'AC': 'documentation/System Architecture.md',
    }
    documents = {path: (root / path).read_text() for path in set(owners.values())}
    known = set()
    for prefix, path in owners.items():
        identifiers = [re.match(r'\| (' + prefix + r'\d+)\b', row).group(1)
                       for row in rows(documents[path], prefix)]
        if not identifiers:
            errors.append(f'{path}: no {prefix} records')
        if len(identifiers) != len(set(identifiers)):
            errors.append(f'{path}: duplicate {prefix} IDs')
        known.update(identifiers)
    current = ['maintainer/FEATURES.md', 'maintainer/ROADMAP.md',
               'maintainer/WORKFLOW.md', 'documentation/Decisions.md']
    for name in current:
        text = documents[name]
        for identifier in set(re.findall(r'\b(?:F|W|U|AC|D|DATA)\d+\b', text)):
            if identifier not in known:
                errors.append(f'{name}: unknown reference {identifier}')
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            if '://' in target:
                continue
            path = unquote(target.split('#')[0])
            resolved = (root / name).parent / path if path else root / name
            if not resolved.is_file():
                errors.append(f'{name}: missing link {target}')
            if '#' in target and resolved.is_file() and resolved.suffix == '.md':
                anchor = unquote(target.split('#', 1)[1])
                if anchor not in heading_ids(resolved.read_text()):
                    errors.append(f'{name}: missing anchor {target}')
            try:
                relative = resolved.resolve().relative_to(root.resolve())
            except ValueError:
                errors.append(f'{name}: link outside repository {target}')
            else:
                if '.local' in relative.parts:
                    errors.append(f'{name}: private link {target}')
    allowed = {
        1: {'Approved', 'Proposed', 'Deferred', 'Superseded'},
        2: {'Implemented', 'Partial', 'Draft', 'Not started', 'Superseded'},
        3: {'Mac dev', 'Source only', 'Setup first', 'Not available', 'N/A'},
    }
    for row in rows(documents['maintainer/FEATURES.md'], 'F'):
        cells = [cell.strip() for cell in row.strip('|').split('|')]
        if len(cells) != 7:
            errors.append(f'Feature needs seven fields: {cells[0]}')
            continue
        for column, choices in allowed.items():
            if cells[column] not in choices:
                errors.append(f'{cells[0]}: invalid state {cells[column]}')
        if any(not cell for cell in cells) or '](' not in cells[6]:
            errors.append(f'{cells[0]}: missing coverage/gap reference')
        if not re.search(r'\bU\d+\b|N/A —', cells[5]):
            errors.append(f'{cells[0]}: missing U reference or N/A reason')
    for script in (root / 'tests').glob('*browser_check.py'):
        if '.local/' in script.read_text():
            errors.append(f'{script.name}: private browser dependency')
    if not (root / 'tests/fixtures/poster.png').is_file():
        errors.append('Missing synthetic poster fixture')
    return errors


if __name__ == '__main__':
    failures = check()
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        sys.exit(1)
    print('Project records: IDs, states, local file links and browser prerequisites passed.')
