#!/usr/bin/env python3
"""Single-terminal SmartCart Quick Tunnel demo supervisor."""
import certifi
import datetime, fcntl, hashlib, http.client, ipaddress, json, os, queue, re, signal, socket, ssl, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PY=Path(sys.executable)
STATE=Path(f'/private/tmp/smartcart-demo-{os.getuid()}')
STATEFILE=STATE/'orchestrator.json'; LOCK=STATE/'orchestrator.lock'
LOGS=ROOT/'logs'; APK=ROOT/'app/build/app/outputs/flutter-apk/app-release.apk'
URL_RE=re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com\b')
DEFAULT_PUBLIC_READINESS_TIMEOUT=180
MIN_PUBLIC_READINESS_TIMEOUT=180
MAX_PUBLIC_READINESS_TIMEOUT=600
SYSTEM_DNS_TIMEOUT=2.0
DOH_REQUEST_TIMEOUT=4.0
DNS_FALLBACK_ATTEMPT_TIMEOUT=20.0
MAX_CNAME_DEPTH=8
children=[]; stopping=False; owns_lock=False

class DemoError(RuntimeError): pass

def out(s): print(s,flush=True)
def concise_error(error):
    reason=getattr(error,'reason',error)
    message=' '.join(str(reason).split())[:140]
    return f'{type(reason).__name__}: {message}' if message else type(reason).__name__

def log_diagnostic(message):
    line=f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {message}"
    out(line)
    try:
        with (LOGS/'orchestrator.log').open('a',encoding='utf-8') as handle:
            handle.write(line+'\n')
    except OSError:
        out('Orchestrator diagnostic could not be appended to logs/orchestrator.log')

def configured_readiness_timeout():
    raw=os.environ.get('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS',str(DEFAULT_PUBLIC_READINESS_TIMEOUT))
    if not re.fullmatch(r'[0-9]{1,4}',raw):
        raise DemoError('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS must be an integer from 180 to 600')
    timeout=int(raw)
    if not MIN_PUBLIC_READINESS_TIMEOUT<=timeout<=MAX_PUBLIC_READINESS_TIMEOUT:
        raise DemoError('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS must be an integer from 180 to 600')
    return timeout

def lsof(port):
    p=subprocess.run(['lsof','-nP',f'-iTCP:{port}','-sTCP:LISTEN'],capture_output=True,text=True)
    return p.stdout.splitlines()[1:]

def terminate_process_group(process, *, grace=8):
    """Stop only the process group owned by a child started in a new session."""
    pgid=process.pid
    try:
        os.killpg(pgid,signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline=time.monotonic()+grace
    while True:
        try:
            os.killpg(pgid,0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        process.poll()  # reap the direct child while checking its descendants
        remaining=deadline-time.monotonic()
        if remaining<=0:
            try:
                os.killpg(pgid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            break
        time.sleep(min(.05,remaining))
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid,signal.SIGKILL)
        except (ProcessLookupError,PermissionError):
            pass
        try: process.wait(timeout=1)
        except subprocess.TimeoutExpired: pass

def release_lock():
    if not owns_lock: return
    for p in reversed(children):
        terminate_process_group(p)
    STATEFILE.unlink(missing_ok=True)
def acquire_lock(path):
    path=Path(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink(): raise DemoError('demo lock file must not be a symlink')
    handle=open(path,'a+')
    os.chmod(path,0o600)
    try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise DemoError('another SmartCart demo command is running') from None
    return handle
def sig(_s,_f):
    global stopping
    stopping=True
def request(url,headers=None):
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers or {}),timeout=4) as r:
        return r.status,json.loads(r.read())

PUBLIC_DNS_RESOLVERS=('https://1.1.1.1/dns-query','https://8.8.8.8/resolve')
def require_verified_https_context(context):
    if context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise DemoError('public HTTPS probe requires certificate and hostname verification')

def create_verified_https_context(ca_bundle=None):
    """Use the maintained certifi roots when the Python install has no CA store."""
    bundle=certifi.where() if ca_bundle is None else str(ca_bundle)
    context=ssl.create_default_context(cafile=bundle)
    require_verified_https_context(context)
    return context

HTTPS_CONTEXT=create_verified_https_context()

class DeadlineExceeded(TimeoutError): pass

def remaining_time(deadline, cap=4.0):
    remaining=deadline-time.monotonic()
    if remaining<=0:
        raise DeadlineExceeded('public readiness deadline expired')
    return remaining if cap is None else min(cap,remaining)

def run_bounded(function, until, *args, **kwargs):
    """Run an uninterruptible blocking operation without blocking shutdown."""
    result=queue.Queue(maxsize=1)
    def run():
        try: result.put((True,function(*args,**kwargs)))
        except BaseException as error: result.put((False,error))
    worker=threading.Thread(target=run,name='smartcart-bounded-network-operation',daemon=True)
    worker.start()
    try:
        success,value=result.get(timeout=remaining_time(until,None))
    except queue.Empty:
        raise DeadlineExceeded('operation exceeded public readiness deadline') from None
    if time.monotonic()>until:
        raise DeadlineExceeded('operation completed after public readiness deadline')
    if success: return value
    raise value

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None

def doh_request(url,headers=None,*,timeout):
    """Issue DoH only to configured HTTPS resolvers and never follow redirects."""
    require_verified_https_context(HTTPS_CONTEXT)
    parsed=urllib.parse.urlsplit(url)
    allowed={urllib.parse.urlsplit(base).netloc+urllib.parse.urlsplit(base).path for base in PUBLIC_DNS_RESOLVERS}
    if parsed.scheme!='https' or parsed.netloc+parsed.path not in allowed or parsed.username or parsed.password or parsed.fragment:
        raise DemoError('DNS-over-HTTPS refused an unconfigured resolver URL')
    if timeout<=0: raise DeadlineExceeded('public readiness deadline expired')
    opener=urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=HTTPS_CONTEXT),NoRedirectHandler(),
    )
    req=urllib.request.Request(url,headers=headers or {})
    try:
        with opener.open(req,timeout=timeout) as response:
            if 300<=response.status<400:
                return response.status,{}
            if response.status!=200:
                return response.status,{}
            content_type=response.headers.get_content_type().lower()
            if content_type not in ('application/dns-json','application/json'):
                raise ValueError(f'unexpected DoH content type: {content_type or "missing"}')
            payload=response.read()
            return response.status,json.loads(payload)
    except urllib.error.HTTPError as error:
        status=error.code
        error.close()
        return status,{}

def system_resolve(host,port):
    """Resolve using the operating system's configured resolver."""
    answers=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    return list(dict.fromkeys(answer[4][0] for answer in answers))

def normalize_dns_name(value):
    if not isinstance(value,str) or not value or len(value)>254 or any(ord(c)<33 or ord(c)>126 for c in value):
        return None
    name=value.rstrip('.').lower()
    labels=name.split('.')
    if (not name or len(name)>253 or any(not label or len(label)>63
            or not re.fullmatch(r'[a-z0-9_-]+',label) for label in labels)):
        return None
    return name

def _resolve_doh_records(host,query_type,expected_type,expected_version,resolver,deadline,query,diagnostic):
    provider='Cloudflare' if '1.1.1.1' in resolver else 'Google'
    current=normalize_dns_name(host)
    if current is None:
        diagnostic(f'{provider} DoH {query_type}: invalid DNS name')
        return []
    visited={current}; cname_depth=0
    while current:
        if time.monotonic()>=deadline: raise DeadlineExceeded(f'{provider} DoH {query_type} exceeded readiness deadline')
        endpoint=resolver+'?name='+urllib.parse.quote(current,safe='')+'&type='+query_type
        try:
            request_timeout=remaining_time(deadline,DOH_REQUEST_TIMEOUT)
            status,body=run_bounded(query,deadline,endpoint,headers={'accept':'application/dns-json'},timeout=request_timeout)
        except DeadlineExceeded:
            raise
        except urllib.error.URLError as error:
            reason=getattr(error,'reason',None)
            if isinstance(reason,ssl.SSLCertVerificationError):
                diagnostic(f'{provider} DoH {query_type} {current}: TLS certificate verification failed ({concise_error(reason)})')
                raise reason
            diagnostic(f'{provider} DoH {query_type} {current}: request failed ({concise_error(error)})')
            return []
        except ssl.SSLCertVerificationError as error:
            diagnostic(f'{provider} DoH {query_type} {current}: TLS certificate verification failed ({concise_error(error)})')
            raise
        except Exception as error:
            diagnostic(f'{provider} DoH {query_type} {current}: request failed ({concise_error(error)})')
            return []
        if status!=200:
            diagnostic(f'{provider} DoH {query_type} {current}: HTTP {status}')
            return []
        if not isinstance(body,dict):
            diagnostic(f'{provider} DoH {query_type} {current}: malformed JSON object')
            return []
        dns_status=body.get('Status')
        if type(dns_status) is not int:
            diagnostic(f'{provider} DoH {query_type} {current}: malformed DNS Status')
            return []
        if dns_status==3:
            diagnostic(f'{provider} DoH {query_type} {current}: NXDOMAIN')
            return []
        if dns_status!=0:
            diagnostic(f'{provider} DoH {query_type} {current}: DNS Status {dns_status}')
            return []
        answers=body.get('Answer',[])
        if not isinstance(answers,list):
            diagnostic(f'{provider} DoH {query_type} {current}: malformed Answer list')
            return []
        records_by_name={}; malformed=0
        for record in answers:
            if not isinstance(record,dict) or type(record.get('type')) is not int:
                malformed+=1
                continue
            owner=normalize_dns_name(record.get('name'))
            if owner is None:
                malformed+=1
                continue
            records_by_name.setdefault(owner,[]).append(record)
        if malformed:
            diagnostic(f'{provider} DoH {query_type} {current}: ignored {malformed} malformed answer record(s)')
        if current not in records_by_name:
            diagnostic(f'{provider} DoH {query_type} {current}: no answers for DNS Status 0')
            return []
        query_target=False
        while True:
            records=records_by_name[current]
            cnames=[record for record in records if record['type']==5]
            address_records=[record for record in records if record['type'] in (1,28)]
            if cnames and address_records:
                diagnostic(f'{provider} DoH {query_type} {current}: conflicting CNAME/address records')
                return []
            found=[]
            for record in records:
                if record['type']!=expected_type: continue
                address=record.get('data')
                if not isinstance(address,str): continue
                try: parsed=ipaddress.ip_address(address)
                except (ValueError,TypeError): continue
                if parsed.version==expected_version: found.append(str(parsed))
            if found:
                diagnostic(f'{provider} DoH {query_type} {host}: resolved {len(found)} {query_type} record(s)')
                return list(dict.fromkeys(found))
            if not cnames:
                break
            targets={normalize_dns_name(record.get('data')) for record in cnames}
            if None in targets or len(targets)!=1:
                diagnostic(f'{provider} DoH {query_type} {current}: malformed or conflicting CNAME target')
                return []
            target=targets.pop()
            if target in visited:
                diagnostic(f'{provider} DoH {query_type}: CNAME loop at {target}')
                return []
            cname_depth+=1
            if cname_depth>MAX_CNAME_DEPTH:
                diagnostic(f'{provider} DoH {query_type}: CNAME depth exceeds {MAX_CNAME_DEPTH}')
                return []
            visited.add(target); current=target
            if current not in records_by_name:
                query_target=True
                break
        if not query_target:
            diagnostic(f'{provider} DoH {query_type} {current}: no usable address answers')
            return []

def public_dns_resolve(host, *, deadline, query=doh_request, diagnostic=out):
    """Resolve A/AAAA records and bounded CNAME chains using configured DoH."""
    results=[]; certificate_errors=[]
    for resolver in PUBLIC_DNS_RESOLVERS:
        for query_type,record_type,version in (('A',1,4),('AAAA',28,6)):
            if time.monotonic()>=deadline: raise DeadlineExceeded('DNS fallback exceeded public readiness deadline')
            try:
                results.extend(_resolve_doh_records(host,query_type,record_type,version,resolver,deadline,query,diagnostic))
            except DeadlineExceeded: raise
            except ssl.SSLCertVerificationError as error:
                certificate_errors.append(error)
                diagnostic(f'{"Cloudflare" if "1.1.1.1" in resolver else "Google"} DoH {query_type} {host}: TLS certificate verification failed ({concise_error(error)})')
            except Exception as error:
                provider='Cloudflare' if '1.1.1.1' in resolver else 'Google'
                diagnostic(f'{provider} DoH {query_type} {host}: rejected response ({concise_error(error)})')
    if not results and certificate_errors:
        raise ssl.SSLCertVerificationError(
            'TLS certificate verification failed for public DNS-over-HTTPS resolvers'
        ) from certificate_errors[-1]
    return list(dict.fromkeys(results))

def https_request_pinned(url,addresses,*,deadline,context=HTTPS_CONTEXT,connector=socket.create_connection):
    """Connect to a resolved IP while retaining URL hostname for TLS SNI/Host."""
    require_verified_https_context(context)
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname is None or parsed.port not in (None,443):
        raise DemoError('public health check refused an invalid HTTPS URL')
    host=parsed.hostname
    path=urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
    last_error=None
    for address in addresses:
        try:
            raw=connector((address,443),timeout=remaining_time(deadline))
            raw.settimeout(remaining_time(deadline))
            with context.wrap_socket(raw,server_hostname=host) as secure:
                secure.settimeout(remaining_time(deadline))
                connection=http.client.HTTPConnection(host,443,timeout=remaining_time(deadline))
                connection.sock=secure
                secure.settimeout(remaining_time(deadline))
                connection.request('GET',path,headers={'Host':host,'Connection':'close'})
                secure.settimeout(remaining_time(deadline))
                response=connection.getresponse()
                secure.settimeout(remaining_time(deadline))
                payload=response.read()
                try:
                    body=json.loads(payload)
                except (UnicodeDecodeError,json.JSONDecodeError):
                    body={}
                return response.status,body
        except urllib.error.HTTPError:
            raise
        except DeadlineExceeded: raise
        except Exception as error:
            last_error=error
    if last_error:
        raise last_error
    raise socket.gaierror('public DNS returned no usable address records')

def fetch_public_health(url, deadline, *, resolve=system_resolve, fallback=public_dns_resolve,
                       request_pinned=https_request_pinned,diagnostic=out):
    host=urllib.parse.urlsplit(url).hostname
    try:
        system_deadline=min(deadline,time.monotonic()+SYSTEM_DNS_TIMEOUT)
        addresses=run_bounded(resolve,system_deadline,host,443)
        diagnostic(f'System DNS {host}: resolved {len(addresses)} address(es)')
    except DeadlineExceeded as error:
        addresses=[]
        diagnostic(f'System DNS {host}: timed out ({error})')
    except (socket.gaierror,OSError) as error:
        addresses=[]
        diagnostic(f'System DNS {host}: failed ({concise_error(error)})')
    if addresses:
        return run_bounded(request_pinned,deadline,url,addresses,deadline=deadline)
    diagnostic(f'Public DNS fallback starting for {host}')
    dns_deadline=min(deadline,time.monotonic()+DNS_FALLBACK_ATTEMPT_TIMEOUT)
    try:
        addresses=fallback(host,deadline=dns_deadline,diagnostic=diagnostic)
    except DeadlineExceeded as error:
        diagnostic(f'Public DNS fallback for {host}: this retry window expired ({error})')
        raise
    if not addresses:
        diagnostic(f'Public DNS fallback found no usable A/AAAA records for {host}')
        raise socket.gaierror(f'system DNS failed and public DNS returned no usable records for {host}')
    diagnostic(f'Public DNS fallback resolved {len(addresses)} address(es) for {host}')
    return run_bounded(request_pinned,deadline,url,addresses,deadline=deadline)

def check_public_health(url, *, fetch=None, sleep=time.sleep, timeout=DEFAULT_PUBLIC_READINESS_TIMEOUT,diagnostic=out):
    if not isinstance(url,str):
        raise DemoError('public health check refused a non-HTTPS Quick Tunnel URL')
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com',parsed.hostname):
        raise DemoError('public health check refused a non-HTTPS Quick Tunnel URL')
    if parsed.port not in (None,443) or parsed.path or parsed.query or parsed.fragment:
        raise DemoError('public health check refused an invalid HTTPS Quick Tunnel URL')
    if fetch is None:
        fetch=lambda endpoint,until:fetch_public_health(endpoint,until,diagnostic=diagnostic)
    endpoint=url+'/health/ready'
    deadline=time.monotonic()+timeout; delay=1.0; last='no response'
    while time.monotonic()<deadline:
        try:
            status,body=fetch(endpoint,deadline)
            if status==200 and body=={'status':'ready'}:
                diagnostic(f'HTTPS readiness {parsed.hostname}: HTTP 200 with ready status')
                return
            last=f'HTTP {status}'
            diagnostic(f'HTTPS readiness {parsed.hostname}: {last}, response not ready')
            if status not in (408,425,429) and status<500:
                raise DemoError(f'public tunnel health endpoint returned {last}; APK was not built')
        except DemoError: raise
        except urllib.error.HTTPError as error:
            last=f'HTTP {error.code}'; error.close()
            diagnostic(f'HTTPS readiness {parsed.hostname}: {last}')
            if error.code not in (408,425,429) and error.code<500:
                raise DemoError(f'public tunnel health endpoint returned {last}; APK was not built') from None
        except (urllib.error.URLError, socket.gaierror, TimeoutError, socket.timeout, ConnectionError) as error:
            reason=getattr(error,'reason',error)
            if isinstance(reason,(TimeoutError,socket.timeout)): last='timeout'
            elif isinstance(reason,(socket.gaierror,)): last=f'DNS lookup failure ({reason})'
            else: last=type(reason).__name__
            diagnostic(f'HTTPS readiness {parsed.hostname}: {last}')
        except Exception as error:
            last=type(error).__name__
            diagnostic(f'HTTPS readiness {parsed.hostname}: request failed ({concise_error(error)})')
        remaining=deadline-time.monotonic()
        if remaining<=0: break
        sleep(min(delay,remaining)); delay=min(delay*1.6,5.0)
    diagnostic(f'HTTPS readiness {parsed.hostname}: not ready after {timeout}s (last result: {last})')
    raise DemoError(f'public tunnel health was not ready after {timeout}s (last result: {last}); APK was not built')
def extract_quick_url(line):
    match=URL_RE.search(line)
    return match.group() if match else None
def start_tunnel_reader(tunnel, log_handle):
    """Drain tunnel stdout while keeping its log handle owned by caller."""
    urls=queue.Queue(); errors=queue.Queue()
    def read_output():
        try:
            for line in tunnel.stdout:
                log_handle.write(line.encode(errors='replace'))
                log_handle.flush()
                found=extract_quick_url(line)
                if found: urls.put(found)
        except Exception as error:
            errors.put(type(error).__name__)
    thread=threading.Thread(target=read_output,name='smartcart-tunnel-log-reader',daemon=True)
    thread.start()
    return thread,urls,errors
def stop_tunnel_reader(tunnel, reader):
    """Stop the owned tunnel and join stdout reader before caller closes logs."""
    terminate_process_group(tunnel)
    reader.join()
    if tunnel.stdout is not None: tunnel.stdout.close()
def build_command(url):
    if not URL_RE.fullmatch(url): raise DemoError('refusing release build with a non-Quick-Tunnel URL')
    return [str(Path.home()/'flutter/bin/flutter'),'build','apk','--release','--no-pub',f'--dart-define=SMARTCART_API_BASE_URL={url}']
def check_backend(p):
    token_file=STATE/'handshake'; end=time.monotonic()+75
    while time.monotonic()<end:
        if p.poll() is not None: raise DemoError(f'backend exited {p.returncode}; see logs/backend.log')
        try:
            t=token_file.read_text().strip()
            if not re.fullmatch('[0-9a-f]{64}',t): raise ValueError()
            status,a=request('http://127.0.0.1:5002/v1/attestation',{'Authorization':'Bearer '+t})
            want={'service':'smartcart-local-demo','status':'ready','listen_host':'127.0.0.1','port':5001,'verified_customer_admission':True,'demo_payment':False}
            if status!=200 or a!=want: raise DemoError('private startup attestation mismatch')
            for path,body in [('/health/live',{'status':'alive'}),('/health/ready',{'status':'ready'})]:
                if request('http://127.0.0.1:5001'+path)!=(200,body): raise DemoError('backend readiness check failed')
            if any('127.0.0.1:' not in x for port in (5001,5002) for x in lsof(port)): raise DemoError('backend listener is not loopback-only')
            return
        except DemoError: raise
        except Exception: time.sleep(.5)
    raise DemoError('backend readiness/attestation timed out; see logs/backend.log')
def validate_apk(url):
    if not URL_RE.fullmatch(url): raise DemoError('APK build refused: URL is not an HTTPS Quick Tunnel URL')
    if not APK.is_file() or APK.stat().st_size<5_000_000: raise DemoError('APK missing or unexpectedly small')
    with zipfile.ZipFile(APK) as z:
        if z.testzip() or not {'classes.dex','AndroidManifest.xml'}<=set(z.namelist()): raise DemoError('APK package validation failed')
    digest=hashlib.sha256(APK.read_bytes()).hexdigest()
    receipt=APK.parent/'smartcart-release-receipt.txt'
    expected=f'api_url={url}\napk_sha256={digest}\n'
    if not receipt.is_file() or receipt.read_text()!=expected: raise DemoError('APK URL/build receipt did not match current tunnel URL')
    return digest
def run_tunnel(tunnel, tunnel_log, backend, readiness_timeout):
    reader,urls,reader_errors=start_tunnel_reader(tunnel,tunnel_log)
    try:
        out('Waiting for Cloudflare Quick Tunnel URL...'); url=None; end=time.monotonic()+90
        while time.monotonic()<end and not stopping:
            if tunnel.poll() is not None: raise DemoError(f'tunnel exited {tunnel.returncode}; see logs/tunnel.log')
            if not reader_errors.empty(): raise DemoError('Cloudflare output reader failed; see logs/tunnel.log')
            try: url=urls.get(timeout=.25); break
            except queue.Empty: pass
        if stopping: return
        if not url or not URL_RE.fullmatch(url): raise DemoError('no valid trycloudflare.com URL; APK was not built')
        if not reader_errors.empty(): raise DemoError('Cloudflare output reader failed; see logs/tunnel.log')
        state=json.loads(STATEFILE.read_text()); state['tunnel_url']=url
        STATEFILE.write_text(json.dumps(state)); os.chmod(STATEFILE,0o600)
        out('Tunnel URL: '+url)
        check_public_health(url,timeout=readiness_timeout,diagnostic=log_diagnostic)
        out('Public tunnel is reaching the ready backend.')
        build_started_ns=time.time_ns()
        with (LOGS/'flutter-build.log').open('ab',buffering=0) as flog:
            build=subprocess.Popen(build_command(url),cwd=ROOT/'app',stdout=flog,stderr=subprocess.STDOUT,start_new_session=True); children.append(build)
            out('Building APK with the current tunnel URL; output is in logs/flutter-build.log...')
            while build.poll() is None and not stopping: time.sleep(.25)
        if stopping: return
        if build.returncode: raise DemoError(f'Flutter build failed ({build.returncode}); see logs/flutter-build.log')
        if not APK.is_file() or APK.stat().st_mtime_ns < build_started_ns:
            raise DemoError('Flutter did not produce a fresh APK for this tunnel URL; existing APK was left untouched')
        receipt=APK.parent/'smartcart-release-receipt.txt'
        digest=hashlib.sha256(APK.read_bytes()).hexdigest()
        receipt.write_text(f'api_url={url}\napk_sha256={digest}\n')
        check=subprocess.run([str(PY),'-B',str(ROOT/'scripts/validate_release_apk.py'),str(APK),url],cwd=ROOT,capture_output=True,text=True)
        if check.returncode: raise DemoError(check.stderr.strip() or 'APK validation script failed')
        out(check.stdout.strip())
        out(f'DEMO READY\nTunnel: {url}\nAPK: {APK}\nLeave this Terminal open. Ctrl-C stops only these services.')
        while not stopping:
            if backend.poll() is not None: raise DemoError(f'backend exited ({backend.returncode})')
            if tunnel.poll() is not None: raise DemoError(f'tunnel exited ({tunnel.returncode})')
            time.sleep(.5)
    finally:
        stop_tunnel_reader(tunnel,reader)
def demo():
    if STATE.is_symlink(): raise DemoError('private state path is a symlink')
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(STATE,0o700)
    lock=acquire_lock(LOCK)
    global owns_lock
    owns_lock=True
    branch=subprocess.run(['git','branch','--show-current'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    if branch!='feature/hardware-integration': raise DemoError('wrong Git branch; expected feature/hardware-integration')
    readiness_timeout=configured_readiness_timeout()
    for port in (5001,5002):
        rows=lsof(port)
        if rows: raise DemoError(f'port {port} is already occupied; stop only a confirmed SmartCart process:\n'+'\n'.join(rows))
    ps=subprocess.run(['/bin/ps','-axo','command='],capture_output=True,text=True)
    if ps.returncode or any('cloudflared tunnel' in x and '--url http://127.0.0.1:5001' in x for x in ps.stdout.splitlines()): raise DemoError('cannot verify tunnel process list or a duplicate tunnel exists')
    for path in (ROOT/'data/smartcart_demo.db',PY,Path.home()/'flutter/bin/flutter'):
        if not path.is_file() or (path==PY and not os.access(path,os.X_OK)): raise DemoError(f'required local file/tool is unavailable: {path.name}')
    import shutil
    if not shutil.which('cloudflared'): raise DemoError('install cloudflared and add it to PATH')
    if not os.access(Path.home()/'flutter/bin/flutter',os.X_OK): raise DemoError('Flutter SDK launcher is not executable')
    pre=subprocess.run([str(PY),'-B','-m','edge.cloud.local','--check'],cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
    if pre.returncode: raise DemoError('local backend preflight failed; see approved .env/Firebase/database configuration')
    if LOGS.is_symlink(): raise DemoError('logs path must not be a symlink')
    LOGS.mkdir(mode=0o700,exist_ok=True); os.chmod(LOGS,0o700)
    out('Resolving Flutter dependencies before starting services...')
    with (LOGS/'flutter-dependencies.log').open('ab') as dep_log:
        deps=subprocess.run([str(Path.home()/'flutter/bin/flutter'),'pub','get'],cwd=ROOT/'app',stdout=dep_log,stderr=subprocess.STDOUT)
    if deps.returncode: raise DemoError(f'Flutter dependency resolution failed ({deps.returncode}); see logs/flutter-dependencies.log')
    if STATEFILE.is_symlink(): raise DemoError('demo state file must not be a symlink')
    STATEFILE.write_text(json.dumps({'pid':os.getpid(),'mode':'smartcart-demo'})); os.chmod(STATEFILE,0o600)
    signal.signal(signal.SIGINT,sig); signal.signal(signal.SIGTERM,sig); signal.signal(signal.SIGHUP,sig)
    with (LOGS/'backend.log').open('ab',buffering=0) as blog:
        out('Starting secure backend...')
        backend=subprocess.Popen(['bash',str(ROOT/'scripts/demo-backend.sh')],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=blog,stderr=subprocess.STDOUT,start_new_session=True); children.append(backend)
        check_backend(backend)
        out('Private attestation and backend readiness passed; port 5002 remains private.')
        with (LOGS/'tunnel.log').open('ab',buffering=0) as tlog:
            tunnel=subprocess.Popen([shutil.which('cloudflared'),'tunnel','--protocol','http2','--url','http://127.0.0.1:5001'],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True); children.append(tunnel)
            run_tunnel(tunnel,tlog,backend,readiness_timeout)
    if stopping: out('Stopping the tunnel and backend started by this command...')
def control(action):
    if action=='logs':
        os.execvp('tail',['tail','-n','100','-F',*[str(LOGS/name) for name in ('backend.log','tunnel.log','orchestrator.log','flutter-build.log','flutter-dependencies.log')]])
    try: state=json.loads(STATEFILE.read_text()); pid=int(state['pid'])
    except Exception: out('SmartCart demo is not running.'); return
    command=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
    if 'demo_orchestrator.py' not in command: out('Demo process cannot be verified; no signal sent.'); return
    if action=='status':
        out(f'SmartCart supervisor is running (PID {pid}).')
        if state.get('tunnel_url'): out('Tunnel URL: '+state['tunnel_url'])
        for port in (5001,5002): out(f'port {port}: '+('listening' if lsof(port) else 'not listening'))
    else: os.kill(pid,signal.SIGTERM); out('Stop requested; supervisor will clean up its own process groups.')
if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1] not in {'demo','status','logs','stop'}: raise SystemExit('usage: demo_orchestrator.py {demo|status|logs|stop}')
    if sys.argv[1]=='demo':
        try: demo()
        except BaseException as exc:
            release_lock()
            if isinstance(exc,KeyboardInterrupt): out('Demo stopped; owned processes cleaned up.')
            else: out(f'DEMO FAILED: {type(exc).__name__}: {exc}'); sys.exit(1)
        finally: release_lock()
    else: control(sys.argv[1])
