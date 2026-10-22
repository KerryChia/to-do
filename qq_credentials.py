"""Current-user Windows DPAPI storage for the local reminder server."""
from __future__ import annotations
import argparse
import csv
import ctypes
from ctypes import wintypes
import getpass
import io
import json
import os
from pathlib import Path
import subprocess
import sys


def credential_path() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise RuntimeError('Private credential directory unavailable')
    return Path(base) / 'ToDoOpenSource' / 'qq.credentials'


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _crypt(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('Use private server environment variables on this platform')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    api = ctypes.windll.crypt32
    if decrypt:
        success = api.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        success = api.CryptProtectData(ctypes.byref(source), 'SemesterPlanner QQ', None, None, None, 1, ctypes.byref(target))
    if not success:
        raise RuntimeError('Credential protection operation failed')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True)
    row = next(csv.reader(io.StringIO(result.stdout.strip())))
    sid = row[-1]
    if not sid.startswith('S-1-'):
        raise RuntimeError('Could not resolve current-user security identity')
    result = subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], capture_output=True)
    if result.returncode:
        raise RuntimeError('Could not restrict credential directory permissions')


def save_credentials(app_id: str, app_secret: str) -> None:
    if not app_id.isdigit() or not (8 <= len(app_secret) <= 256) or any(c.isspace() for c in app_secret):
        raise ValueError('Invalid credential format')
    path = credential_path()
    if path.exists():
        raise RuntimeError('Credentials already exist; explicit rotation is required')
    _private_directory(path.parent)
    sealed = _crypt(json.dumps({'appId': app_id, 'appSecret': app_secret}).encode('utf-8'))
    with path.open('xb') as handle:
        handle.write(sealed)


def add_credentials(app_id: str, app_secret: str) -> None:
    if not app_id.isdigit() or not isinstance(app_secret, str) or not (8 <= len(app_secret) <= 256) or any(c.isspace() for c in app_secret):
        raise ValueError('Invalid credential format')
    path = credential_path()
    data = json.loads(_crypt(path.read_bytes(), decrypt=True))
    bots = data.get('bots') or [{'slot': 'bot-1', 'appId': data['appId'], 'appSecret': data['appSecret']}]
    if len(bots) >= 2 or any(b['appId'] == app_id for b in bots):
        raise RuntimeError('Bot slot exists or two-slot limit reached')
    bots.append({'slot': 'bot-2', 'appId': app_id, 'appSecret': app_secret})
    _private_directory(path.parent)
    sealed = _crypt(json.dumps({'bots': bots}).encode('utf-8'))
    temporary = path.with_suffix('.next')
    with temporary.open('xb') as handle:
        handle.write(sealed)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_into_env() -> bool:
    if os.environ.get('QQBOT_BOTS'):
        return True
    if os.environ.get('QQBOT_APP_ID') and os.environ.get('QQBOT_APP_SECRET'):
        os.environ['QQBOT_BOTS'] = json.dumps([{'slot':'bot-1','appId':os.environ['QQBOT_APP_ID'],'appSecret':os.environ['QQBOT_APP_SECRET']}])
        return True
    if os.name != 'nt':
        return False
    path = credential_path()
    if not path.exists():
        return False
    try:
        data = json.loads(_crypt(path.read_bytes(), decrypt=True))
        bots = data.get('bots') or [{'slot':'bot-1','appId':data.get('appId'),'appSecret':data.get('appSecret')}]
        if not isinstance(bots, list) or not 1 <= len(bots) <= 2:
            return False
        if any(not isinstance(b.get('appId'),str) or not isinstance(b.get('appSecret'),str) for b in bots):
            return False
        os.environ['QQBOT_BOTS'] = json.dumps(bots)
        os.environ['QQBOT_APP_ID'] = bots[0]['appId']
        os.environ['QQBOT_APP_SECRET'] = bots[0]['appSecret']
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure-json', action='store_true', help='Read one credential JSON object from standard input; never echo it')
    parser.add_argument('--configure', action='store_true', help='Prompt for application credentials without echoing the secret')
    parser.add_argument('--add-json', action='store_true', help='Add a second bot via private stdin without replacing the first')
    args = parser.parse_args()
    try:
        if args.add_json:
            print('Waiting for additional private credential input.', flush=True)
            data = json.loads(sys.stdin.readline())
            add_credentials(str(data.get('appId', '')), data.get('appSecret', ''))
            print('Second bot protected; first bot preserved.')
        elif args.configure_json:
            print('Waiting for private credential input.', flush=True)
            data = json.loads(sys.stdin.readline())
            save_credentials(str(data.get('appId', '')), data.get('appSecret', ''))
            print('Credentials protected for the current Windows user.')
        elif args.configure:
            save_credentials(input('AppID: ').strip(), getpass.getpass('AppSecret: ').strip())
            print('Credentials protected for the current Windows user.')
        else:
            print(json.dumps({'configured': load_into_env(), 'storage': 'Windows DPAPI' if os.name == 'nt' else 'server environment'}))
        return 0
    except Exception:
        print('Credential operation failed. No credential values were logged.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
