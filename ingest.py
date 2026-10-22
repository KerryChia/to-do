"""Bounded local extraction + API import. Task understanding belongs to the agent."""
from pathlib import Path
import argparse, datetime as dt, hashlib, json, os, re, sys, unicodedata, zipfile
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
MAX_FILE = 32 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_BATCH = 4 * 1024 * 1024
MAX_FILES = 500
CHUNK = 12000
EXT = {'.txt', '.md', '.csv', '.docx', '.pptx', '.pdf', '.doc', '.ppt', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.xlsx'}
EXCLUDE = {'待办日历', 'node_modules', 'cache', '__pycache__', '.git', '.agents', '.zcode'}
STATUSES = {'processed', 'no_tasks', 'needs_review', 'failed'}
REMIND = {-1, 0, 1440, 2880, 4320, 7200, 10080, 20160, 43200}


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def log(event, **counts):
    path = HERE / 'data' / 'import-log.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'at': stamp(), 'event': event, **counts}, ensure_ascii=False) + '\n')
    return path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('api_redirect_refused')


class API:
    def __init__(self, base, access_password=None, *, allow_remote=False):
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.path not in {'', '/'} or parts.query or parts.fragment:
            raise ValueError('api_base_invalid')
        if parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('http_allowed_loopback_only')
        if parts.hostname not in {'localhost', '127.0.0.1', '::1'} and not allow_remote:
            raise ValueError('remote_api_requires_explicit_approval')
        self.base = base.rstrip('/')
        self.access_password = access_password
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, route, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if self.access_password:
            headers['X-Planner-Access'] = self.access_password
        if token:
            headers['X-Planner-Token'] = token
        req = urllib.request.Request(self.base + '/api/v1/' + route, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers=headers)
        with self.opener.open(req, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError('api_response_limit')
        return json.loads(data)


def hidden(path):
    return path.name.startswith(('.', '~$')) or bool(getattr(path.stat(follow_symlinks=False), 'st_file_attributes', 0) & 2)


def safe_source(root, relative):
    p = Path(relative)
    if p.is_absolute() or not p.parts or any(x in {'..', '.'} or x in EXCLUDE or x.startswith(('.', '~$')) for x in p.parts) or ':' in relative or '\\' in relative:
        raise ValueError('unsafe_source_path')
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()) or hidden(current):
            raise ValueError('unsafe_source_link')
    current.resolve().relative_to(root.resolve())
    if current.resolve().is_relative_to(HERE):
        raise ValueError('project_source_excluded')
    if current.suffix.lower() not in EXT:
        raise ValueError('unsupported_source')
    return current


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def xml(z, name):
    raw = z.read(name)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('unsafe_xml')
    return ET.fromstring(raw)


def local(tag):
    return tag.rsplit('}', 1)[-1]


def paragraphs(node):
    for p in node.iter():
        if local(p.tag) == 'p':
            text = ''.join((n.text or '') if local(n.tag) == 't' else ('\t' if local(n.tag) == 'tab' else '\n') for n in p.iter() if local(n.tag) in {'t', 'tab', 'br'})
            if text.strip():
                yield text


def extract(path):
    if path.stat().st_size > MAX_FILE:
        return [], ['file_size_limit']
    ext = path.suffix.lower()
    warnings, rows = [], []
    if ext in {'.doc', '.ppt'}:
        return [], ['requires_conversion']
    if ext in {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'}:
        return [], ['needs_ocr']
    if ext == '.xlsx':
        return [], ['requires_spreadsheet_review']
    if ext in {'.txt', '.md', '.csv'}:
        raw = path.read_bytes()
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return [], ['unknown_encoding']
        rows = [{'locator': f'line:{i}', 'text': s} for i, s in enumerate(text.splitlines(), 1)]
    elif ext in {'.docx', '.pptx'}:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > 64 * 1024 * 1024 or any(i.file_size > 16 * 1024 * 1024 or i.file_size / max(i.compress_size, 1) > 500 for i in infos):
                raise ValueError('zip_limit')
            names = set(z.namelist())
            if any('/media/' in n or '/embeddings/' in n for n in names):
                warnings.append('embedded_media_review')
            if ext == '.docx':
                names_read = ['word/document.xml'] + sorted(n for n in names if re.fullmatch(r'word/(header\d+|footer\d+|footnotes|endnotes|comments)\.xml', n))
                for name in names_read:
                    for i, text in enumerate(paragraphs(xml(z, name)), 1):
                        rows.append({'locator': f'{name}:paragraph:{i}', 'text': text})
            else:
                # presentation.xml relationship order, not slide filename order.
                rels = {n.attrib['Id']: n.attrib['Target'] for n in xml(z, 'ppt/_rels/presentation.xml.rels') if n.attrib.get('TargetMode') != 'External'}
                pres = xml(z, 'ppt/presentation.xml')
                for i, node in enumerate((n for n in pres.iter() if local(n.tag) == 'sldId'), 1):
                    rid = next(v for k, v in node.attrib.items() if k.endswith('}id'))
                    target = rels[rid]
                    name = target.lstrip('/') if target.startswith('/') else 'ppt/' + target
                    for j, text in enumerate(paragraphs(xml(z, name)), 1):
                        rows.append({'locator': f'slide:{i}:paragraph:{j}', 'text': text})
                    relname = str(Path(name).parent).replace('\\', '/') + '/_rels/' + Path(name).name + '.rels'
                    if relname in names:
                        import posixpath
                        for rel in xml(z, relname):
                            if rel.attrib.get('Type', '').endswith('/notesSlide') and rel.attrib.get('TargetMode') != 'External':
                                target = rel.attrib['Target']
                                note = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                                for j, text in enumerate(paragraphs(xml(z, note)), 1):
                                    rows.append({'locator': f'slide:{i}:speaker-note:{j}', 'text': text})
    elif ext == '.pdf':
        try:
            import pymupdf
        except ImportError:
            try:
                from pypdf import PdfReader
            except ImportError:
                return [], ['pdf_reader_unavailable']
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                return [], ['encrypted_pdf']
            if len(reader.pages) > 1000:
                return [], ['page_limit']
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                rows.append({'locator': f'page:{i}', 'text': text})
                if not text.strip():
                    warnings.append('needs_ocr')
            warnings.append('pdf_visual_review')
        else:
            with pymupdf.open(path) as doc:
                if doc.needs_pass:
                    return [], ['encrypted_pdf']
                if len(doc) > 1000:
                    return [], ['page_limit']
                for i, page in enumerate(doc, 1):
                    text = page.get_text()
                    rows.append({'locator': f'page:{i}', 'text': text})
                    if not text.strip():
                        warnings.append('needs_ocr')
                    elif page.get_images() or page.get_drawings():
                        warnings.append('pdf_visual_review')
    if sum(len(r['text']) for r in rows) > MAX_TEXT:
        return [], ['text_limit']
    if not any(r['text'].strip() for r in rows):
        warnings.append('empty_text_review')
    return rows, sorted(set(warnings))


def scan(root, prior, out):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError('scan_root_missing')
    if root.is_relative_to(HERE):
        raise ValueError('project_source_excluded')
    old = {f['path']: f for f in prior}
    results, skipped = [], 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE and (Path(parent) / d).resolve() != out.resolve() and not hidden(Path(parent) / d) and not (Path(parent) / d).is_symlink() and not (hasattr(Path(parent) / d, 'is_junction') and (Path(parent) / d).is_junction()))
        for name in sorted(files):
            p = Path(parent) / name
            if p.suffix.lower() not in EXT or p.is_symlink() or hidden(p):
                continue
            relative = p.relative_to(root).as_posix()
            safe_source(root, relative)
            sha = digest(p)
            previous = old.get(relative, {})
            if previous.get('sha256') == sha and previous.get('status') in {'processed', 'no_tasks'}:
                skipped += 1
                continue
            if len(results) >= MAX_FILES:
                raise ValueError('scan_file_limit_split_root')
            try:
                rows, warnings = extract(p)
                error = None
            except Exception:
                rows, warnings, error = [], ['extraction_failed'], 'extraction_failed'
            if digest(p) != sha:
                rows, warnings, error = [], ['source_changed_during_scan'], 'source_changed_during_scan'
            chunks, buf, size = [], [], 0
            def flush():
                nonlocal buf, size
                if buf:
                    dest = out / 'docs' / f'{len(results):04d}-{len(chunks):04d}.json'
                    dump(dest, {'path': relative, 'sha256': sha, 'segments': buf})
                    chunks.append(dest.relative_to(out).as_posix())
                    buf, size = [], 0
            for row in rows:
                for start in range(0, max(1, len(row['text'])), CHUNK // 2):
                    part = {'locator': row['locator'], 'offset': start, 'text': row['text'][start:start + CHUNK // 2]}
                    count = len(json.dumps(part, ensure_ascii=False))
                    if size + count > CHUNK:
                        flush()
                    buf.append(part)
                    size += count
            flush()
            results.append({'path': relative, 'sha256': sha, 'status': 'failed' if error else 'needs_review', 'warnings': warnings, 'chunks': chunks, 'error': error})
    manifest = {'version': 1, 'root': str(root), 'createdAt': stamp(), 'files': results, 'skippedUnchanged': skipped}
    dump(out / 'manifest.json', manifest)
    return manifest


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def task_id(path, task):
    key = '\0'.join(norm(x) for x in [path, task['category'], task['kind'], task['title']])
    return 'import_' + hashlib.sha256(key.encode()).hexdigest()[:40]


def validate(batch, manifest):
    if not isinstance(batch, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', batch.get('batchId', '')):
        raise ValueError('invalid_batch_id')
    files = batch.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('invalid_file_count')
    known = {f['path']: f for f in manifest['files']}
    seen, total = set(), 0
    for f in files:
        path = f.get('path')
        if path in seen or path not in known:
            raise ValueError('unknown_or_duplicate_source')
        seen.add(path)
        origin = known[path]
        if f.get('sha256') != origin['sha256'] or not re.fullmatch('[0-9a-f]{64}', f['sha256']):
            raise ValueError('source_hash_mismatch')
        p = safe_source(Path(manifest['root']).resolve(), path)
        if digest(p) != f['sha256']:
            raise ValueError('source_changed_rescan')
        if f.get('status') not in STATUSES or not isinstance(f.get('tasks'), list):
            raise ValueError('invalid_file_status')
        if f['status'] in {'no_tasks', 'failed', 'needs_review'} and f['tasks']:
            raise ValueError('status_task_conflict')
        if f['status'] == 'processed' and not f['tasks']:
            raise ValueError('use_no_tasks')
        if origin.get('warnings') and f['status'] in {'processed', 'no_tasks'}:
            if not isinstance(f.get('reviewResolution'), str) or len(f['reviewResolution'].strip()) < 20:
                raise ValueError('review_resolution_required')
            if any(w in origin['warnings'] for w in ['requires_conversion', 'needs_ocr', 'extraction_failed', 'source_changed_during_scan', 'file_size_limit', 'text_limit', 'page_limit', 'encrypted_pdf', 'pdf_reader_unavailable']):
                raise ValueError('unresolved_extraction_rescan_required')
        if 'error' in f and f['error'] is not None and not re.fullmatch('[a-z_]{1,80}', f['error']):
            raise ValueError('unsafe_error')
        for t in f['tasks']:
            total += 1
            if total > 1000:
                raise ValueError('task_limit')
            for field, limit in {'title': 200, 'category': 100, 'notes': 10000, 'source': 1000, 'sourceNotes': 3000, 'sourceTiming': 2000}.items():
                if not isinstance(t.get(field), str) or len(t[field]) > limit:
                    raise ValueError('invalid_task_text')
            if not t['title'].strip() or not t['category'].strip() or t['source'] != path or not t['sourceNotes'].strip():
                raise ValueError('missing_task_evidence')
            if type(t.get('priority')) is not int or t['priority'] not in range(1, 5) or type(t.get('priorityDefault')) is not bool:
                raise ValueError('invalid_priority')
            if t['priorityDefault'] and t['priority'] != 3:
                raise ValueError('default_priority_must_be_3')
            if t.get('kind') not in {'assignment', 'milestone', 'suggestion', 'manual'}:
                raise ValueError('invalid_kind')
            due = t.get('due')
            if due != '':
                if not isinstance(due, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', due):
                    raise ValueError('invalid_due')
                dt.datetime.strptime(due, '%Y-%m-%dT%H:%M')
            if type(t.get('remind')) is not int or t['remind'] not in REMIND or type(t.get('duration')) is not int or not 1 <= t['duration'] <= 1440:
                raise ValueError('invalid_schedule')
            if type(t.get('completed')) is not bool or t.get('firedKey') != '':
                raise ValueError('invalid_task_state')
            for field in ('createdAt', 'updatedAt'):
                if not isinstance(t.get(field), str) or not t[field].endswith('Z'):
                    raise ValueError('invalid_timestamp')
                dt.datetime.fromisoformat(t[field].replace('Z', '+00:00'))
            t['id'] = task_id(path, t)
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scan')
    s.add_argument('--root', type=Path, required=True, help='Explicit directory to scan (no automatic parent/Desktop scan)')
    s.add_argument('--api', default='http://127.0.0.1:8766')
    s.add_argument('--allow-remote-api', action='store_true', help='Explicitly authorize credentials/content to another HTTPS API host')
    s.add_argument('--access-password-file', type=Path, help='File containing the shared API access password (never passed on the command line)')
    s.add_argument('--out', type=Path)
    s = sub.add_parser('submit')
    s.add_argument('--batch', type=Path, required=True)
    s.add_argument('--manifest', type=Path, required=True)
    s.add_argument('--api', default='http://127.0.0.1:8766')
    s.add_argument('--allow-remote-api', action='store_true', help='Explicitly authorize credentials/content to another HTTPS API host')
    s.add_argument('--access-password-file', type=Path, help='File containing the shared API access password (never passed on the command line)')
    s.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        password = None
        if getattr(args, 'access_password_file', None):
            password = args.access_password_file.read_text(encoding='utf-8-sig').strip()
            if not password:
                raise ValueError('empty_access_password')
        if args.command == 'scan':
            api = API(args.api, password, allow_remote=args.allow_remote_api)
            prior = api.request('imports')['files']
            out = args.out or HERE / 'data' / 'ingest-runs' / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            if out.exists():
                raise ValueError('output_already_exists')
            result = scan(args.root, prior, out)
            logfile = log('scan', candidates=len(result['files']), skipped=result['skippedUnchanged'])
            print(json.dumps({'manifest': str((out / 'manifest.json').resolve()), 'candidates': len(result['files']), 'skipped': result['skippedUnchanged'], 'log': str(logfile)}, ensure_ascii=False))
        else:
            if args.batch.stat().st_size > MAX_BATCH or args.manifest.stat().st_size > MAX_BATCH:
                raise ValueError('batch_size_limit')
            batch = validate(json.loads(args.batch.read_text(encoding='utf-8-sig')), json.loads(args.manifest.read_text(encoding='utf-8-sig')))
            if args.dry_run:
                print(json.dumps({'valid': True, 'fileCount': len(batch['files']), 'taskCount': sum(len(f['tasks']) for f in batch['files'])}))
                return
            api = API(args.api, password, allow_remote=args.allow_remote_api)
            # Fetch existing records for agent-side reconciliation; API performs final dedupe atomically.
            api.request('tasks')
            token = api.request('session')['token']
            payload = {'batchId': batch['batchId'], 'files': [{k: v for k, v in f.items() if k in {'path', 'sha256', 'status', 'tasks', 'error'} and v is not None} for f in batch['files']]}
            result = api.request('imports', payload, token)
            counts = {k: result.get(k) for k in ('added', 'skipped', 'fileCount', 'revision')}
            receipt = args.batch.with_suffix('.receipt.json')
            dump(receipt, {'batchId': batch['batchId'], 'at': stamp(), **counts})
            logfile = log('submit_success', **counts)
            print(json.dumps({**counts, 'receipt': str(receipt.resolve()), 'log': str(logfile)}, ensure_ascii=False))
    except Exception as exc:
        # Exception messages can contain server content, source text or sensitive paths.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z_]{1,80}', str(exc)) else type(exc).__name__
        logfile = log('operation_failed', code=code)
        print(json.dumps({'error': code, 'log': str(logfile)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
