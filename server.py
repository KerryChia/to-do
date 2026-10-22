"""Loopback task API and strictly whitelisted static assets; Python standard library only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs
from urllib.request import urlopen
import argparse
import json
import mimetypes
import os
import secrets
import hashlib
import hmac
import time
import threading
from http.cookies import SimpleCookie, CookieError
import socket
import ipaddress
import re
import sys
import webbrowser
from backend import Store, APIError, require, TIMEZONE

ROOT = Path(__file__).resolve().parent
HOST, PORT = '127.0.0.1', 8766
IDENTITY = b'{"app":"semester-task-calendar","version":1}'
# Pages share one app shell; app.js shows the section matching location.pathname.
PAGES = {'/': 'index.html', '/tasks': 'index.html', '/calendar': 'index.html', '/qq': 'index.html', '/data': 'index.html'}
ASSETS = {'index.html','styles.css','utils.js','app.js'}
API_PREFIX = '/api/'
MAX_BODY = 12*1024*1024


def resolve_loopback(address):
    host, port = address
    if not 0 <= port <= 65535:
        raise ValueError('Bind port must be between 0 and 65535')
    try:
        addresses = socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError('Bind address could not be resolved') from None
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_loopback for info in addresses):
        raise ValueError('Bind address must resolve only to loopback addresses')
    return addresses[0][0], addresses[0][4][0]


def validate_public_origin(value):
    if value is None:
        return None
    error = 'PLANNER_PUBLIC_ORIGIN must be an exact HTTP(S) origin with a valid hostname, no credentials or trailing slash'
    try:
        if not value or any(c.isspace() or ord(c)<33 or ord(c)==127 for c in value):
            raise ValueError(error)
        origin = urlsplit(value)
        host, port = origin.hostname, origin.port
        if (origin.scheme not in {'http','https'} or not host or origin.username is not None or origin.password is not None
                or origin.path or origin.query or origin.fragment or '?' in value or '#' in value or '\\' in value
                or value != origin.scheme+'://'+origin.netloc or origin.netloc.endswith(':')
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError(error)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host)>253 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in host.split('.')):
                raise ValueError(error)
        return value
    except ValueError:
        raise ValueError(error) from None


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def __init__(self,address,handler=None,store=None,access_password=None,trust_proxy=False,public_origin=None):
        self.address_family, host = resolve_loopback(address)
        public_origin = validate_public_origin(public_origin if public_origin is not None else os.environ.get('PLANNER_PUBLIC_ORIGIN'))
        if trust_proxy and (not access_password or not public_origin or urlsplit(public_origin).scheme!='https'):
            raise ValueError('--trust-proxy requires an access password and explicit HTTPS PLANNER_PUBLIC_ORIGIN on loopback')
        self.store = store
        self.reminders = None
        self.token = secrets.token_urlsafe(32)
        # Keep only the digest bytes around; the plaintext password never persists.
        self.access_password = hashlib.sha256(access_password.encode('utf-8')).digest() if access_password else None
        self.cookie_key = secrets.token_bytes(32)
        if store:
            self.configure_cookie_key(store.data_dir)
        self.public_origin = public_origin
        self.login_attempts = {}
        self.revoked = {}
        self.auth_lock = threading.Lock()
        self.trust_proxy = trust_proxy
        super().__init__((host,address[1]),handler or Handler)

    def configure_cookie_key(self,data_dir):
        path = Path(data_dir)/'session-secret'
        try:
            fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            secret = path.read_bytes()
        else:
            secret = secrets.token_bytes(32)
            with os.fdopen(fd,'wb') as file:
                file.write(secret)
        if len(secret)!=32:
            raise ValueError('Invalid session signing secret')
        self.cookie_key = hmac.digest(secret,b'planner-session-v1'+(self.access_password or b''),'sha256')

    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

    def handle_error(self,request,client_address):
        if self.store:
            self.store.audit('request_error',{'status':'internal_error'})

class Handler(BaseHTTPRequestHandler):
    server_version = 'Planner/1'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self,*args):
        pass

    def send_error(self,code,message=None,explain=None):
        self.respond(code,{'error':'http_error'})

    def respond(self,status,value,content_type='application/json; charset=utf-8',cookie=None):
        data = value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        if cookie:
            self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Cross-Origin-Resource-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.close_connection = True
        if self.command!='HEAD':
            self.wfile.write(data)

    def session_value(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie',''))
            value = cookies['planner_session'].value
            expires, nonce, signature = value.split('.')
            message = expires+'.'+nonce
            expected = hmac.new(self.server.cookie_key,message.encode('ascii'),hashlib.sha256).hexdigest()
            if len(nonce)!=32 or not secrets.compare_digest(signature,expected) or int(expires)<=time.time():
                return None
            with self.server.auth_lock:
                if nonce in self.server.revoked:
                    return None
            return value
        except (KeyError,ValueError,UnicodeError,CookieError):
            return None

    def session_cookie(self,value='',max_age=0):
        secure = (self.server.public_origin or '').startswith('https://') or (self.server.trust_proxy and self.headers.get('X-Forwarded-Proto')=='https')
        return f'planner_session={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax'+('; Secure' if secure else '')

    def check_login_rate(self):
        # Trust no client-supplied forwarding address. Behind a proxy this is a
        # conservative shared budget. Bounded state prevents address churn DoS.
        now = time.monotonic()
        with self.server.auth_lock:
            attempts = self.server.login_attempts
            for key in list(attempts):
                if attempts[key][0]<=now-60:
                    del attempts[key]
            key = self.client_address[0]
            require(key in attempts or len(attempts)<1024,'login_rate_limited',429)
            start,count = attempts.get(key,(now,0))
            require(count<10,'login_rate_limited',429)
            attempts[key] = (start,count+1)

    def access(self,path):
        if not self.server.access_password or (path=='/api/v1/session' and self.command=='POST'):
            return
        if not (path.startswith(API_PREFIX) or path==API_PREFIX.rstrip('/')):
            return
        values = self.headers.get_all('X-Planner-Access',[])
        if len(values)==1 and secrets.compare_digest(hashlib.sha256(values[0].encode('utf-8',errors='surrogatepass')).digest(),self.server.access_password):
            return
        require(bool(self.session_value()),'access_required',401)

    def auth(self,path):
        hosts = self.headers.get_all('Host',[])
        port = self.server.server_address[1]
        if self.server.trust_proxy:
            # Reverse proxy holds the only public listener; this process is loopback-bound.
            # The proxy forwards the public Host header unchanged; Origin must match it.
            require(len(hosts)==1 and bool(hosts[0]) and (not self.server.public_origin or hosts[0]==urlsplit(self.server.public_origin).netloc),'invalid_host',403)
            if self.command in {'POST','PATCH','DELETE','PUT'}:
                origins = self.headers.get_all('Origin',[])
                forwarded = self.headers.get_all('X-Forwarded-Proto',[])
                scheme = forwarded[0] if len(forwarded)==1 and forwarded[0] in {'http','https'} else ''
                require(scheme and (not self.server.public_origin or scheme==urlsplit(self.server.public_origin).scheme),'invalid_origin',403)
                require(not origins or (len(origins)==1 and origins[0]==scheme+'://'+hosts[0]),'invalid_origin',403)
                require(self.headers.get('Sec-Fetch-Site') not in {'cross-site','same-site'},'invalid_origin',403)
        else:
            bound_host = self.server.server_address[0]
            bound_host = '['+bound_host+']' if ':' in bound_host else bound_host
            require(len(hosts)==1 and hosts[0] in {f'127.0.0.1:{port}',f'localhost:{port}',f'[::1]:{port}',f'{bound_host}:{port}'},'invalid_host',403)
            if self.command in {'POST','PATCH','DELETE','PUT'}:
                origins = self.headers.get_all('Origin',[])
                require(not origins or (len(origins)==1 and origins[0]=='http://'+hosts[0]),'invalid_origin',403)
                require(self.headers.get('Sec-Fetch-Site') not in {'cross-site','same-site'},'invalid_origin',403)
        self.access(path)
        if self.command in {'POST','PATCH','DELETE','PUT'} and path!='/api/v1/session':
            tokens = self.headers.get_all('X-Planner-Token',[])
            require(len(tokens)==1 and secrets.compare_digest(tokens[0],self.server.token),'invalid_token',403)

    def body(self):
        require(not self.headers.get('Transfer-Encoding'),'unsupported_transfer_encoding')
        require(not self.headers.get('Content-Encoding'),'unsupported_content_encoding')
        lengths = self.headers.get_all('Content-Length',[])
        require(len(lengths)==1 and lengths[0].isascii() and lengths[0].isdigit(),'length_required',411)
        size = int(lengths[0])
        require(size<=MAX_BODY,'body_too_large',413)
        require(self.headers.get('Content-Type','').split(';')[0].strip().lower()=='application/json','json_required',415)
        raw = self.rfile.read(size)
        require(len(raw)==size,'incomplete_body')
        def pairs(items):
            result = {}
            for k,v in items:
                require(k not in result,'duplicate_json_key')
                result[k]=v
            return result
        def invalid_constant(value):
            raise APIError(400,'invalid_json')
        try:
            return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid_constant)
        except (ValueError,UnicodeError,RecursionError):
            raise APIError(400,'invalid_json')

    def dispatch(self):
        try:
            decoded_path = unquote(urlsplit(self.path).path,errors='strict')
            self.auth(decoded_path)
            parsed = urlsplit(self.path)
            require(not parsed.scheme and not parsed.netloc,'invalid_target')
            path = unquote(parsed.path,errors='strict')
            method, store = self.command,self.server.store
            if method=='GET':
                if path=='/__planner_health':
                    return self.respond(200,IDENTITY)
                if path=='/api/v1/health':
                    snap = store.snapshot()
                    return self.respond(200,{'ok':True,'version':1,'taskCount':len(snap['tasks']),'revision':snap['revision'],'timezone':TIMEZONE,'storage':'sqlite','accessRequired':bool(self.server.access_password)})
                if path=='/api/v1/session' and method=='GET':
                    return self.respond(200,{'token':self.server.token})
                if path=='/api/v1/schedule':
                    with store.lock:
                        return self.respond(200,store.schedule.snapshot())
                if path=='/api/v1/tasks':
                    return self.respond(200,store.snapshot())
                if path=='/api/v1/qq/status':
                    return self.respond(200,self.server.reminders.status() if self.server.reminders else {'configured':False,'bound':False,'gatewayReady':False,'enabled':False,'deliveryStatus':'not_configured','timezone':TIMEZONE,'platformNotice':'QQ service not configured','bots':[]})
                if path=='/api/v1/imports':
                    return self.respond(200,store.imports())
                if path=='/api/v1/events':
                    query = parse_qs(parsed.query)
                    try:
                        limit = int(query.get('limit',['100'])[0])
                    except ValueError:
                        raise APIError(400,'invalid_limit')
                    return self.respond(200,store.events(limit))
                page = PAGES.get(path if path!='/' else '/')
                if page:
                    asset = ROOT/page
                    require(not asset.is_symlink() and asset.resolve().parent==ROOT,'not_found',404)
                    return self.respond(200,asset.read_bytes(),mimetypes.guess_type(page)[0] or 'text/html')
                name = path[1:]
                require(path.startswith('/') and path.count('/')==1 and name in ASSETS,'not_found',404)
                asset = ROOT/name
                require(not asset.is_symlink() and asset.resolve().parent==ROOT,'not_found',404)
                return self.respond(200,asset.read_bytes(),mimetypes.guess_type(name)[0] or 'application/octet-stream')
            if path=='/api/v1/session' and method=='POST':
                self.check_login_rate()
                payload = self.body()
                require(type(payload) is dict and 'accessPassword' in payload and set(payload)<={'accessPassword','username'},'invalid_login')
                require('username' not in payload or payload['username']=='planner','invalid_login',401)
                supplied = payload['accessPassword']
                require(type(supplied) is str and len(supplied)<=1024,'invalid_login')
                ok = self.server.access_password is None or secrets.compare_digest(hashlib.sha256(supplied.encode('utf-8')).digest(),self.server.access_password)
                require(ok,'invalid_credentials',401)
                max_age = 30*24*60*60
                message = str(int(time.time())+max_age)+'.'+secrets.token_hex(16)
                value = message+'.'+hmac.new(self.server.cookie_key,message.encode('ascii'),hashlib.sha256).hexdigest()
                return self.respond(200,{'token':self.server.token},cookie=self.session_cookie(value,max_age))
            if path=='/api/v1/logout' and method=='POST':
                self.body()
                value = self.session_value()
                if value:
                    expires,nonce,_ = value.split('.')
                    with self.server.auth_lock:
                        self.server.revoked = {k:v for k,v in self.server.revoked.items() if v>time.time()}
                        if len(self.server.revoked)>=4096:
                            self.server.cookie_key = secrets.token_bytes(32)
                            self.server.revoked.clear()
                        self.server.revoked[nonce] = int(expires)
                return self.respond(200,{'ok':True},cookie=self.session_cookie())
            if path=='/api/v1/schedule' and method=='POST':
                payload=self.body()
                require(type(payload) is dict and set(payload)=={'ics'} and isinstance(payload['ics'],str),'invalid_calendar')
                try:
                    with store.lock:
                        result=store.schedule.import_ics(payload['ics'])
                except (ValueError,KeyError):
                    raise APIError(400,'invalid_calendar')
                store.audit('schedule_import',{'count':result['count']})
                return self.respond(200,result)
            if path=='/api/v1/deadlines/resolve' and method=='POST':
                payload=self.body()
                require(type(payload) is dict and {'category','phrase'}<=set(payload) and set(payload)<={'category','phrase','sourceDate'},'invalid_deadline_request')
                require(isinstance(payload['category'],str) and isinstance(payload['phrase'],str),'invalid_deadline_request')
                try:
                    with store.lock:
                        result=store.schedule.resolve(payload['category'],payload['phrase'],payload.get('sourceDate'))
                except (ValueError,TypeError):
                    raise APIError(400,'invalid_deadline_request')
                return self.respond(200,result)
            if path=='/api/v1/tasks' and method in {'POST','PUT'}:
                result = store.create(self.body()) if method=='POST' else store.replace(self.body())
                return self.respond(201 if method=='POST' else 200,result)
            if path.startswith('/api/v1/tasks/') and method in {'PATCH','DELETE'}:
                id = path[len('/api/v1/tasks/'):]
                require(bool(id) and '/' not in id,'not_found',404)
                return self.respond(200,store.change(id,self.body(),method=='DELETE'))
            if path in {'/api/v1/qq/test','/api/v1/qq/pairing'} and method=='POST':
                body = self.body()
                is_test = path.endswith('/test')
                require(type(body) is dict and set(body)<={'bot'},'invalid_qq_request')
                require(is_test or 'bot' in body,'bot_required')
                slot = body.get('bot')
                require('bot' not in body or (type(slot) is str and slot in ('bot-1','bot-2')),'invalid_bot')
                service = self.server.reminders
                result = (service.test(slot) if is_test else service.pairing(slot)) if service else {'ok':False,'status':'not_configured'}
                return self.respond(200,result)
            if path=='/api/v1/imports' and method=='POST':
                return self.respond(200,store.import_batch(self.body()))
            raise APIError(404,'not_found')
        except APIError as e:
            self.respond(e.status,{'error':e.code})
        except (UnicodeError,ValueError,RecursionError):
            self.respond(400,{'error':'invalid_request'})
        except (BrokenPipeError,ConnectionResetError,socket.timeout):
            self.close_connection = True
        except Exception:
            if self.server.store:
                self.server.store.audit('request_error',{'status':'internal_error'})
            self.respond(500,{'error':'internal_error'})

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = dispatch

    def do_HEAD(self):
        self.respond(405,{'error':'method_not_allowed'})

    do_OPTIONS = do_HEAD

def load_access_password(args):
    if args.access_password_file:
        try:
            raw = Path(args.access_password_file).read_text(encoding='utf-8')
        except OSError:
            print('Access password file could not be read.',file=sys.stderr)
            sys.exit(1)
        if os.name=='posix':
            mode = Path(args.access_password_file).stat().st_mode & 0o777
            if mode & 0o077:
                print('Access password file must not be readable by group or others (chmod 600).',file=sys.stderr)
                sys.exit(1)
        lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if len(lines)!=1:
            print('Access password file must contain exactly one non-empty line.',file=sys.stderr)
            sys.exit(1)
        return lines[0]
    return os.environ.get('PLANNER_ACCESS_PASSWORD') or None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open',action='store_true')
    parser.add_argument('--bind',default=os.environ.get('PLANNER_BIND') or HOST)
    parser.add_argument('--port',type=int,default=os.environ.get('PLANNER_PORT') or PORT)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('PLANNER_DATA_DIR') or ROOT/'data'))
    parser.add_argument('--no-seed',action='store_true',help='Deprecated compatibility flag (no-op); new databases always start empty')
    parser.add_argument('--no-reminders',action='store_true',help='Disable the QQ reminder scheduler for this instance')
    parser.add_argument('--access-password-file',default=None,help='File with exactly one password line; must be chmod 600 on Linux')
    parser.add_argument('--trust-proxy',action='store_true',default=None,help='Public origin served by a reverse proxy that forwards Host unchanged')
    args = parser.parse_args()
    trust_proxy = args.trust_proxy if args.trust_proxy is not None else os.environ.get('PLANNER_TRUST_PROXY')=='1'
    access_password = load_access_password(args)
    try:
        _, host = resolve_loopback((args.bind,args.port))
    except ValueError as exc:
        parser.error(str(exc))
    url_host = '['+host+']' if ':' in host else host
    url = f'http://{url_host}:{args.port}/'
    # Bind before touching persistent data; another instance must not initialize it.
    try:
        server = LocalServer((host,args.port),access_password=access_password,trust_proxy=trust_proxy)
    except ValueError as exc:
        parser.error(str(exc))
    except OSError:
        try:
            # Ungated identity endpoint: /api/v1/health is behind the access gate when set.
            with urlopen(url+'__planner_health',timeout=2) as response:
                status = json.load(response)
                same_app = status.get('app')=='semester-task-calendar'
        except Exception:
            same_app = False
        if same_app:
            print('Planner already running: '+url)
            if args.open:
                webbrowser.open(url)
            return
        print('Port occupied by another service or old static planner. Nothing stopped or opened.',file=sys.stderr)
        sys.exit(1)
    try:
        server.store = Store(args.data_dir)
        server.configure_cookie_key(server.store.data_dir)
        if not args.no_reminders:
            try:
                from qq_credentials import load_into_env
                from qq_reminders import MultiReminderService
                load_into_env()
                server.reminders = MultiReminderService(task_provider=server.store.snapshot_tasks,data_dir=server.store.data_dir,audit=server.store.audit)
                server.reminders.start()
            except Exception:
                server.store.audit('qq_start',{'status':'unavailable'})
        print('Task calendar running at '+url,flush=True)
        print('Bound to '+host+'; SQLite persistence; timezone Asia/Shanghai. Ctrl+C to stop.',flush=True)
        if access_password:
            print('Shared access password: ENABLED (browser session cookie or agent access header).',flush=True)
        if trust_proxy:
            print('Reverse proxy mode: Host/Origin checks follow the public origin.',flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.reminders:
            server.reminders.stop()
        server.server_close()
        if server.store:
            server.store.close()

if __name__=='__main__':
    main()
