"""Check the exact source allowlist and common accidental disclosure patterns."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = set('''
.gitattributes
.gitignore
.github/workflows/tests.yml
API.md
LICENSE
README.md
SECURITY.md
app.js
backend.py
config/qq.env.example
course_schedule.py
deploy/to-do.service.example
docs/DEPLOY.md
docs/QQ.md
fixtures/sample-schedule.ics
index.html
ingest-tests.py
ingest.py
qq_credentials.py
qq_gateway.mjs
qq_reminders.py
scripts/check_release.py
server.py
styles.css
task.schema.json
templates/import-batch.example.json
tests.cjs
tests_api.py
tests_qq.py
tests_schedule.py
tests_security.py
utils.js
'''.split())
PATTERNS = {
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'cloud_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'credential_url': re.compile(r'https?://[^/\s:@]+:[^/\s@]+@[A-Za-z0-9.-]+'),
    'absolute_user_path': re.compile(r'(?:[A-Z]:[/\\](?:Users|Documents and Settings)[/\\]|(?<![A-Za-z0-9_/])/(?:home|Users)/[^/\s]+/)', re.I),
}


def tracked_files():
    result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode:
        return sorted(str(p.relative_to(ROOT)).replace('\\', '/') for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    if Path(result.stdout.strip()).resolve() != ROOT:
        raise ValueError('Release must have an independent repository root')
    files = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'])
    return sorted(p.decode('utf-8') for p in files.split(b'\0') if p)


def main():
    files = tracked_files()
    issues = []
    for name in sorted(set(files) - ALLOWED):
        issues.append(f'{name}: not in reviewed source allowlist')
    for name in sorted(ALLOWED - set(files)):
        issues.append(f'{name}: missing reviewed source file')
    for name in files:
        path = ROOT / name
        if path.is_symlink():
            issues.append(f'{name}: symbolic link refused')
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            issues.append(f'{name}: unreadable or non-text release file')
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    if label == 'credential_url':
                        url = urlsplit(match.group())
                        host = url.hostname or ''
                        if host.endswith(('.example', '.invalid', '.test')) and url.username == 'user' and url.password in {'pass', 'secret'}:
                            continue
                    issues.append(f'{name}:{number}: {label}')
    if issues:
        print('\n'.join(issues), file=sys.stderr)
        return 1
    print(f'PASS: {len(files)} reviewed source files; no blocked paths or common credential patterns.')
    print('This is a heuristic gate, not a substitute for reviewing the staged diff and history.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
