import sys
from pathlib import Path
import subprocess
import io
import urllib.error
import urllib.parse
import socket
import ssl
import json
import http.server
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import demo_orchestrator as demo


@pytest.mark.parametrize('line,url', [
    ('Tunnel ready: https://random-name-123.trycloudflare.com', 'https://random-name-123.trycloudflare.com'),
    ('no tunnel url yet', None),
    ('https://bad.example.com', None),
    ('http://random.trycloudflare.com', None),
])
def test_quick_tunnel_url_capture_accepts_only_https_expected_domain(line, url):
    assert demo.extract_quick_url(line) == url


def test_release_build_injects_exact_current_tunnel_url():
    url = 'https://current-567.trycloudflare.com'
    args = demo.build_command(url)
    assert f'--dart-define=SMARTCART_API_BASE_URL={url}' in args
    assert args[:4] == [str(Path.home()/'flutter/bin/flutter'), 'build', 'apk', '--release']
    with pytest.raises(demo.DemoError):
        demo.build_command('https://stale.example.com')


def test_public_health_retries_transient_dns_and_edge_failures():
    outcomes = [
        urllib.error.URLError(socket.gaierror('temporary system DNS failure')),
        urllib.error.URLError(ConnectionResetError('temporary edge connection reset')),
        (502, {'error': 'edge starting'}),
        (200, {'status': 'ready'}),
    ]
    calls = []
    def fetch(url):
        calls.append(url)
        result = outcomes.pop(0)
        if isinstance(result, Exception): raise result
        return result
    demo.check_public_health(
        'https://demo-123.trycloudflare.com', fetch=lambda url, _deadline: fetch(url), sleep=lambda _: None,
    )
    assert len(calls) == 4
    assert all(url == 'https://demo-123.trycloudflare.com/health/ready' for url in calls)


def test_system_dns_failure_uses_public_dns_and_keeps_hostname_url():
    observed = {}
    def resolve(host, port):
        observed['system'] = (host, port)
        raise socket.gaierror('system resolver unavailable')
    def fallback(host,*,deadline,diagnostic):
        observed['fallback'] = host
        return ['203.0.113.8']
    def pinned(url, addresses, *, deadline):
        observed['pinned'] = (url, addresses)
        return 200, {'status': 'ready'}
    demo.check_public_health(
        'https://demo-123.trycloudflare.com',
        fetch=lambda endpoint, deadline: demo.fetch_public_health(
            endpoint, deadline, resolve=resolve,
            fallback=fallback, request_pinned=pinned,
        ),
        sleep=lambda _: None,
    )
    assert observed == {
        'system': ('demo-123.trycloudflare.com', 443),
        'fallback': 'demo-123.trycloudflare.com',
        'pinned': ('https://demo-123.trycloudflare.com/health/ready', ['203.0.113.8']),
    }


def test_public_dns_without_usable_records_is_reported_and_retried():
    calls = []
    now = [0.0]
    def fetch(endpoint, deadline):
        calls.append(endpoint)
        return demo.fetch_public_health(
                endpoint, deadline,
            resolve=lambda *_: (_ for _ in ()).throw(socket.gaierror('system DNS failed')),
            fallback=lambda _,**__: [],
        )
    def sleep(seconds): now[0] += seconds
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(demo.time, 'monotonic', lambda: now[0])
    try:
        with pytest.raises(demo.DemoError, match='public DNS returned no usable records'):
            demo.check_public_health('https://demo-123.trycloudflare.com', fetch=fetch,
                                     sleep=sleep, timeout=2.5)
    finally:
        monkeypatch.undo()
    assert len(calls) >= 2


def make_local_cert(tmp_path, common_name, san):
    key=tmp_path/'key.pem'; cert=tmp_path/'cert.pem'
    subprocess.run([
        'openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),
        '-out',str(cert),'-days','1','-subj',f'/CN={common_name}',
        '-addext',f'subjectAltName={san}',
    ],check=True,capture_output=True,text=True)
    return cert,key

class LocalTLSResponse(http.server.BaseHTTPRequestHandler):
    status=200; location=None; received_hosts=[]; received_paths=[]
    def do_GET(self):
        type(self).received_hosts.append(self.headers.get('Host'))
        type(self).received_paths.append(self.path)
        self.send_response(type(self).status)
        if type(self).location: self.send_header('Location',type(self).location)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(b'{"status":"ready"}')
    def log_message(self,*_args): pass

def start_tls_server(cert,key):
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),LocalTLSResponse)
    server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    sni=[]
    context.set_servername_callback(lambda _sock,name,_ctx:sni.append(name))
    server.socket=context.wrap_socket(server.socket,server_side=True)
    LocalTLSResponse.status=200; LocalTLSResponse.location=None
    LocalTLSResponse.received_hosts=[]; LocalTLSResponse.received_paths=[]
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    return server,thread,sni

def local_connector(port, seen):
    def connect(address,timeout):
        seen.append((address,timeout))
        return socket.create_connection(('127.0.0.1',port),timeout=timeout)
    return connect

def test_pinned_https_uses_real_hostname_validation_sni_and_host_header(tmp_path):
    host='demo-123.trycloudflare.com'
    cert,key=make_local_cert(tmp_path,host,f'DNS:{host}')
    server,thread,sni=start_tls_server(cert,key)
    connector_calls=[]
    context=ssl.create_default_context(cafile=str(cert))
    try:
        result=demo.https_request_pinned(
            f'https://{host}/health/ready',['203.0.113.8'],deadline=time.monotonic()+3,
            context=context,connector=local_connector(server.server_port,connector_calls),
        )
        assert result==(200,{'status':'ready'})
        assert connector_calls[0][0]==('203.0.113.8',443)
        assert sni==[host]
        assert LocalTLSResponse.received_hosts==[host]
        assert LocalTLSResponse.received_paths==['/health/ready']
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)

def test_pinned_https_rejects_untrusted_and_wrong_hostname_certificates(tmp_path):
    host='demo-123.trycloudflare.com'
    cert,key=make_local_cert(tmp_path,host,f'DNS:{host}')
    server,thread,_sni=start_tls_server(cert,key)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            demo.https_request_pinned(
                f'https://{host}/health/ready',['203.0.113.8'],deadline=time.monotonic()+3,
                connector=local_connector(server.server_port,[]),
            )
        trusted=ssl.create_default_context(cafile=str(cert))
        with pytest.raises(ssl.SSLCertVerificationError):
            demo.https_request_pinned(
                'https://other-123.trycloudflare.com/health/ready',['203.0.113.8'],
                deadline=time.monotonic()+3,context=trusted,
                connector=local_connector(server.server_port,[]),
            )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)

def test_pinned_https_rejects_unverified_context_before_connecting():
    unverified=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname=False
    unverified.verify_mode=ssl.CERT_NONE
    assert unverified.verify_mode==ssl.CERT_NONE and not unverified.check_hostname
    called=[]
    with pytest.raises(demo.DemoError,match='requires certificate and hostname verification'):
        demo.https_request_pinned(
            'https://demo-123.trycloudflare.com/health/ready',['203.0.113.8'],
            deadline=time.monotonic()+1,context=unverified,connector=lambda *_:called.append(1),
        )
    assert called==[]

def test_verified_context_uses_certifi_ca_bundle(monkeypatch):
    bundle=demo.certifi.where(); calls=[]
    create_default_context=ssl.create_default_context
    monkeypatch.setattr(demo.certifi,'where',lambda:bundle)
    monkeypatch.setattr(demo.ssl,'create_default_context',lambda **kwargs:(
        calls.append(kwargs) or create_default_context(**kwargs)
    ))
    context=demo.create_verified_https_context()
    assert calls==[{'cafile':bundle}]
    assert context.verify_mode==ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_public_dns_json_discards_invalid_records_and_accepts_valid_ip_families():
    def query(url, headers, timeout):
        assert url.startswith('https://1.1.1.1/dns-query?') or url.startswith('https://8.8.8.8/resolve?')
        assert headers == {'accept': 'application/dns-json'}
        assert 0<timeout<=4
        return 200, {'Status':0,'Answer': [
            {'name':'demo-123.trycloudflare.com','type': 1, 'data': '203.0.113.9'},
            {'name':'demo-123.trycloudflare.com','type': 28, 'data': '2001:db8::1'},
        ]}
    assert demo.public_dns_resolve('demo-123.trycloudflare.com', deadline=time.monotonic()+2, query=query) == ['203.0.113.9','2001:db8::1']

def test_google_doh_uses_resolve_json_endpoint_and_parses_a_and_aaaa():
    calls=[]
    host='demo-123.trycloudflare.com'
    def query(url,*,headers,timeout):
        parsed=urllib.parse.urlsplit(url)
        params=urllib.parse.parse_qs(parsed.query)
        calls.append((parsed.scheme,parsed.netloc,parsed.path,params,headers,timeout))
        if parsed.netloc=='1.1.1.1':
            assert parsed.path=='/dns-query'
            return 503,{}
        assert parsed.netloc=='8.8.8.8'
        assert parsed.path=='/resolve'
        assert params=={'name':[host],'type':[params['type'][0]]}
        assert params['type'][0] in ('A','AAAA')
        assert headers=={'accept':'application/dns-json'}
        assert 0<timeout<=4
        if params['type'][0]=='A':
            return 200,{'Status':0,'Answer':[{'name':host,'type':1,'data':'104.16.230.132'}]}
        return 200,{'Status':0,'Answer':[{'name':host,'type':28,'data':'2606:4700::6810:e684'}]}

    answers=demo.public_dns_resolve(host,deadline=time.monotonic()+3,query=query)
    assert answers==['104.16.230.132','2606:4700::6810:e684']
    google_calls=[call for call in calls if call[1]=='8.8.8.8']
    assert [(call[2],call[3]['name'],call[3]['type'],call[4]) for call in google_calls]==[
        ('/resolve',[host],['A'],{'accept':'application/dns-json'}),
        ('/resolve',[host],['AAAA'],{'accept':'application/dns-json'}),
    ]

@pytest.mark.parametrize('response',[(400,{}),(200,'<html>not-json</html>'),(200,{'Status':0,'Answer':'not-a-list'})])
def test_google_doh_http_or_malformed_response_fails_safely(response):
    def query(url,**_kwargs):
        if urllib.parse.urlsplit(url).netloc=='1.1.1.1': return 503,{}
        return response
    assert demo.public_dns_resolve(
        'demo-123.trycloudflare.com',deadline=time.monotonic()+2,query=query,
    )==[]

@pytest.mark.parametrize('response', [
    {'Status': 0, 'Answer': [{'type': True, 'data': '203.0.113.9'}]},
    {'Status': 0, 'Answer': [{'type': '1', 'data': '203.0.113.9'}]},
    {'Answer': [{'type': 1, 'data': '203.0.113.9'}]},
    {'Status': True, 'Answer': [{'type': 1, 'data': '203.0.113.9'}]},
    {'Status': 3, 'Answer': [{'type': 1, 'data': '203.0.113.9'}]},
    {'Status': 0, 'Answer': [
            {'type': 1, 'data': 'not-an-ip'},
            {'type': 1, 'data': 3405803785},
            {'type': 28, 'data': 'bad::address'},
            {'type': 16, 'data': 'irrelevant'},
        ]},
])
def test_public_dns_rejects_malformed_or_unsuccessful_answers(response):
    def query(_url,**_kwargs): return 200,response
    assert demo.public_dns_resolve(
        'demo-123.trycloudflare.com',deadline=time.monotonic()+2,query=query,
    )==[]

def test_dns_a_cname_chain_with_address_in_same_response(monkeypatch):
    host='demo-123.trycloudflare.com'; target='edge.example.net'
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',('https://1.1.1.1/dns-query',))
    def query(url,**_kwargs):
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if q['type']==['A']:
            return 200,{'Status':0,'Answer':[
                {'name':host+'.','type':5,'data':target+'.'},
                {'name':target+'.','type':1,'data':'104.16.230.132'},
            ]}
        return 200,{'Status':0,'Answer':[]}
    assert demo.public_dns_resolve(host,deadline=time.monotonic()+2,query=query,diagnostic=lambda _:None)==['104.16.230.132']

def test_dns_aaaa_cname_chain_queries_target(monkeypatch):
    host='demo-123.trycloudflare.com'; target='edge-v6.example.net'; queried=[]
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',('https://8.8.8.8/resolve',))
    def query(url,**_kwargs):
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query); name=q['name'][0]; kind=q['type'][0]
        queried.append((name,kind))
        if kind=='AAAA' and name==host:
            return 200,{'Status':0,'Answer':[{'name':host,'type':5,'data':target+'.'}]}
        if kind=='AAAA' and name==target:
            return 200,{'Status':0,'Answer':[{'name':target+'.','type':28,'data':'2606:4700::6810:e684'}]}
        return 200,{'Status':0,'Answer':[]}
    assert demo.public_dns_resolve(host,deadline=time.monotonic()+2,query=query,diagnostic=lambda _:None)==['2606:4700::6810:e684']
    assert (target,'AAAA') in queried

def test_dns_cname_loop_and_excessive_depth_are_rejected(monkeypatch):
    host='demo-123.trycloudflare.com'
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',('https://1.1.1.1/dns-query',))
    loop={'A':{host:{'Status':0,'Answer':[{'name':host,'type':5,'data':'alias.example.net'}]},
               'alias.example.net':{'Status':0,'Answer':[{'name':'alias.example.net','type':5,'data':host}]}}}
    def loop_query(url,**_kwargs):
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return 200,loop[q['type'][0]][q['name'][0]]
    assert demo.public_dns_resolve(host,deadline=time.monotonic()+2,query=loop_query,diagnostic=lambda _:None)==[]

    def deep_query(url,**_kwargs):
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query); name=q['name'][0]; kind=q['type'][0]
        if kind!='A': return 200,{'Status':0,'Answer':[]}
        index=0 if name==host else int(name.split('.')[0].removeprefix('alias'))
        return 200,{'Status':0,'Answer':[{'name':name,'type':5,'data':f'alias{index+1}.example.net'}]}
    assert demo.public_dns_resolve(host,deadline=time.monotonic()+2,query=deep_query,diagnostic=lambda _:None)==[]

@pytest.mark.parametrize('response', [
    {'Status':3,'Answer':[{'name':'demo-123.trycloudflare.com','type':1,'data':'104.16.230.132'}]},
    {'Status':0,'Answer':[]},
    {'Status':0,'Answer':'malformed'},
])
def test_dns_nxdomain_empty_and_malformed_responses_are_not_success(response,monkeypatch):
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',('https://1.1.1.1/dns-query',))
    def query(_url,**_kwargs): return 200,response
    assert demo.public_dns_resolve('demo-123.trycloudflare.com',deadline=time.monotonic()+2,query=query,diagnostic=lambda _:None)==[]

def test_both_doh_providers_failing_is_logged_and_returns_no_addresses():
    calls=[]; messages=[]
    def query(url,**_kwargs):
        calls.append(urllib.parse.urlsplit(url).netloc)
        raise urllib.error.URLError('resolver unavailable')
    assert demo.public_dns_resolve('demo-123.trycloudflare.com',deadline=time.monotonic()+2,query=query,diagnostic=messages.append)==[]
    assert set(calls)=={'1.1.1.1','8.8.8.8'}
    assert any('Cloudflare DoH' in line and 'request failed' in line for line in messages)
    assert any('Google DoH' in line and 'request failed' in line for line in messages)

def test_doh_certificate_failures_are_not_reported_as_dns_absence():
    messages=[]
    error=ssl.SSLCertVerificationError('certificate verify failed')
    def query(_url,**_kwargs): raise urllib.error.URLError(error)
    with pytest.raises(ssl.SSLCertVerificationError,match='TLS certificate verification failed'):
        demo.public_dns_resolve(
            'demo-123.trycloudflare.com',deadline=time.monotonic()+2,
            query=query,diagnostic=messages.append,
        )
    assert any('TLS certificate verification failed' in message for message in messages)
    assert not any('no usable A/AAAA records' in message for message in messages)

def test_delayed_dns_publication_retries_without_restarting_tunnel():
    attempts={'fallback':0}; messages=[]
    def resolver(*_args): raise socket.gaierror('system DNS unavailable')
    def fallback(_host,*,deadline,diagnostic):
        attempts['fallback']+=1
        diagnostic(f'test resolver attempt {attempts["fallback"]}')
        return [] if attempts['fallback']==1 else ['104.16.230.132']
    def pinned(url,addresses,*,deadline):
        assert url=='https://demo-123.trycloudflare.com/health/ready'
        assert addresses==['104.16.230.132']
        return 200,{'status':'ready'}
    def fetch(url,deadline):
        return demo.fetch_public_health(url,deadline,resolve=resolver,fallback=fallback,
                                        request_pinned=pinned,diagnostic=messages.append)
    demo.check_public_health('https://demo-123.trycloudflare.com',fetch=fetch,
                             sleep=lambda _:None,timeout=2,diagnostic=messages.append)
    assert attempts['fallback']==2
    assert any('test resolver attempt 1' in message for message in messages)
    assert any('test resolver attempt 2' in message for message in messages)

def test_public_readiness_timeout_defaults_to_180_and_is_configurable(monkeypatch):
    monkeypatch.delenv('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS',raising=False)
    assert demo.configured_readiness_timeout()==180
    monkeypatch.setenv('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS','240')
    assert demo.configured_readiness_timeout()==240
    monkeypatch.setenv('SMARTCART_PUBLIC_READINESS_TIMEOUT_SECONDS','60')
    with pytest.raises(demo.DemoError,match='180 to 600'):
        demo.configured_readiness_timeout()

def test_doh_redirect_is_rejected_without_following_it(tmp_path,monkeypatch):
    cert,key=make_local_cert(tmp_path,'localhost','IP:127.0.0.1')
    server,thread,_sni=start_tls_server(cert,key)
    LocalTLSResponse.status=302; LocalTLSResponse.location='/redirected'
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',(f'https://127.0.0.1:{server.server_port}/dns-query',))
    context=ssl.create_default_context(cafile=str(cert))
    original_build_opener=urllib.request.build_opener
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers: original_build_opener(
        urllib.request.HTTPSHandler(context=context),*handlers,
    ))
    try:
        status,body=demo.doh_request(
            f'https://127.0.0.1:{server.server_port}/dns-query?name=x&type=A',
            timeout=1,
        )
        assert status==302 and body=={}
        assert LocalTLSResponse.received_paths==['/dns-query?name=x&type=A']
        assert demo.NoRedirectHandler().redirect_request(None,None,302,'Found',{},'/redirected') is None
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)

def test_both_doh_providers_use_configured_verified_ca_bundle(tmp_path,monkeypatch):
    cert,key=make_local_cert(tmp_path,'127.0.0.1','IP:127.0.0.1')
    class DnsHandler(http.server.BaseHTTPRequestHandler):
        paths=[]
        def do_GET(self):
            type(self).paths.append(self.path.split('?',1)[0])
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            name=params['name'][0]
            if params['type']==['A']:
                answer={'name':name,'type':1,'data':'104.16.230.132'}
            else:
                answer={'name':name,'type':28,'data':'2606:4700::6810:e684'}
            payload=json.dumps({'Status':0,'Answer':[answer]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/dns-json')
            self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self,*_args): pass

    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),DnsHandler)
    server.daemon_threads=True
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); server_context.load_cert_chain(cert,key)
    server.socket=server_context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    trusted=ssl.create_default_context(cafile=str(cert))
    base=f'https://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(demo,'HTTPS_CONTEXT',trusted)
    monkeypatch.setattr(demo,'PUBLIC_DNS_RESOLVERS',(base+'/cloudflare',base+'/google'))
    try:
        answers=demo.public_dns_resolve(
            'demo-123.trycloudflare.com',deadline=time.monotonic()+3,diagnostic=lambda _:None,
        )
        assert answers==['104.16.230.132','2606:4700::6810:e684']
        assert set(DnsHandler.paths)=={'/cloudflare','/google'}
        assert trusted.verify_mode==ssl.CERT_REQUIRED and trusted.check_hostname is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)

def test_doh_rejects_unverified_configured_context_before_connecting(monkeypatch):
    unverified=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname=False; unverified.verify_mode=ssl.CERT_NONE
    monkeypatch.setattr(demo,'HTTPS_CONTEXT',unverified)
    with pytest.raises(demo.DemoError,match='certificate and hostname verification'):
        demo.doh_request('https://1.1.1.1/dns-query?name=x&type=A',timeout=1)

def test_doh_timeout_is_capped_by_remaining_deadline():
    now=time.monotonic(); observed=[]
    def query(_url,*,headers,timeout):
        observed.append(timeout)
        time.sleep(.5)
        return 200,{'Answer':[]}
    with pytest.raises(demo.DeadlineExceeded):
        demo.public_dns_resolve('demo-123.trycloudflare.com',deadline=now+.08,query=query)
    assert observed and 0<observed[0]<=.08

def test_stalled_system_dns_cannot_block_deadline_or_shutdown():
    started=time.monotonic()
    def stalled_resolver(*_args): time.sleep(.5); return []
    with pytest.raises(socket.gaierror,match='public DNS returned no usable records'):
        demo.fetch_public_health(
            'https://demo-123.trycloudflare.com/health/ready',started+.08,
            resolve=stalled_resolver,fallback=lambda *_args,**_kwargs:[],
        )
    assert time.monotonic()-started<.25
    workers=[t for t in threading.enumerate() if t.name=='smartcart-bounded-network-operation']
    assert workers and all(t.daemon for t in workers)

@pytest.mark.parametrize('stage', ['connect','tls','http'])
def test_connection_tls_and_http_stages_obey_total_deadline(stage,monkeypatch):
    started=time.monotonic(); deadline=started+.08
    class Sock:
        def settimeout(self,timeout): assert 0<timeout<=deadline-time.monotonic()+.01
        def __enter__(self): return self
        def __exit__(self,*_args): pass
    class VerifiedContext:
        verify_mode=ssl.CERT_REQUIRED
        check_hostname=True
        def wrap_socket(self,_sock,server_hostname):
            assert server_hostname=='demo-123.trycloudflare.com'
            if stage=='tls': time.sleep(.5)
            return Sock()
    class Response:
        status=200
        def read(self):
            if stage=='http': time.sleep(.5)
            return b'{"status":"ready"}'
    class Connection:
        sock=None
        def __init__(self,*_args,**_kwargs): pass
        def request(self,*_args,**_kwargs): pass
        def getresponse(self): return Response()
    def connector(_address,timeout):
        assert 0<timeout<=deadline-time.monotonic()+.01
        if stage=='connect': time.sleep(.5)
        return Sock()
    monkeypatch.setattr(demo.http.client,'HTTPConnection',Connection)
    with pytest.raises(demo.DeadlineExceeded):
        demo.run_bounded(
            demo.https_request_pinned,deadline,
            'https://demo-123.trycloudflare.com/health/ready',['203.0.113.8'],
            deadline=deadline,context=VerifiedContext(),connector=connector,
        )
    assert time.monotonic()-started<.25


def test_public_health_fails_fast_for_nonretryable_http_and_invalid_url():
    calls = []
    with pytest.raises(demo.DemoError, match='HTTP 404'):
        demo.check_public_health(
            'https://demo-123.trycloudflare.com',
            fetch=lambda *_: (404, {}), sleep=lambda _: None,
        )
    with pytest.raises(demo.DemoError, match='non-HTTPS Quick Tunnel'):
        demo.check_public_health('http://demo-123.trycloudflare.com', fetch=lambda _: calls.append(1))
    with pytest.raises(demo.DemoError, match='invalid HTTPS Quick Tunnel'):
        demo.check_public_health('https://demo-123.trycloudflare.com:444', fetch=lambda _: calls.append(1))
    with pytest.raises(demo.DemoError, match='invalid HTTPS Quick Tunnel'):
        demo.check_public_health('https://demo-123.trycloudflare.com/path', fetch=lambda _: calls.append(1))
    assert calls == []


def test_tunnel_log_reader_is_joined_before_log_handle_closes():
    tunnel = subprocess.Popen(
        [sys.executable, '-c',
         "import time; print('https://reader-test.trycloudflare.com', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    log = io.BytesIO()
    reader, urls, errors = demo.start_tunnel_reader(tunnel, log)
    try:
        assert urls.get(timeout=3) == 'https://reader-test.trycloudflare.com'
    finally:
        demo.stop_tunnel_reader(tunnel, reader)
        log.close()
    assert not reader.is_alive()
    assert errors.empty()
    assert tunnel.poll() is not None


def test_owned_process_group_is_cleaned_on_shutdown(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    state_file = tmp_path / 'orchestrator.json'
    state_file.write_text('{}')
    monkeypatch.setattr(demo, 'children', [child])
    monkeypatch.setattr(demo, 'STATEFILE', state_file)
    monkeypatch.setattr(demo, 'owns_lock', True)
    demo.release_lock()
    assert child.poll() is not None
    assert not state_file.exists()

def test_cleanup_kills_group_descendant_after_direct_leader_exits(tmp_path,monkeypatch):
    pid_file=tmp_path/'descendant.pid'
    leader_code=(
        'import pathlib,subprocess,sys; '
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
        'pathlib.Path(sys.argv[1]).write_text(str(child.pid))'
    )
    leader=subprocess.Popen(
        [sys.executable,'-c',leader_code,str(pid_file)],start_new_session=True,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    )
    state_file=tmp_path/'orchestrator.json'; state_file.write_text('{}')
    terminate=demo.terminate_process_group
    try:
        leader.wait(timeout=3)
        descendant=int(pid_file.read_text())
        assert subprocess.run(['/bin/ps','-p',str(descendant),'-o','stat='],capture_output=True,text=True).stdout.strip()
        monkeypatch.setattr(demo,'children',[leader])
        monkeypatch.setattr(demo,'STATEFILE',state_file)
        monkeypatch.setattr(demo,'owns_lock',True)
        monkeypatch.setattr(demo,'terminate_process_group',lambda process:terminate(process,grace=1))
        started=time.monotonic()
        demo.release_lock()
        assert time.monotonic()-started<3
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            state=subprocess.run(['/bin/ps','-p',str(descendant),'-o','stat='],capture_output=True,text=True).stdout.strip()
            if not state or state.startswith('Z'): break
            time.sleep(.05)
        assert not state or state.startswith('Z')
        assert not state_file.exists()
    finally:
        terminate(leader,grace=.2)


def test_failed_lock_owner_does_not_remove_existing_state(tmp_path, monkeypatch):
    state_file = tmp_path / 'orchestrator.json'
    state_file.write_text('{"pid":1}')
    monkeypatch.setattr(demo, 'STATEFILE', state_file)
    monkeypatch.setattr(demo, 'children', [])
    monkeypatch.setattr(demo, 'owns_lock', False)
    demo.release_lock()
    assert state_file.read_text() == '{"pid":1}'


def test_second_process_cannot_acquire_demo_lock(tmp_path):
    lock_path = tmp_path / 'demo.lock'
    held = demo.acquire_lock(lock_path)
    module_path = str(Path(__file__).parents[1] / 'scripts')
    code = f'''import sys
from pathlib import Path
sys.path.insert(0, {module_path!r})
import demo_orchestrator as d
try:
    d.acquire_lock(Path(sys.argv[1]))
except d.DemoError:
    raise SystemExit(42)
raise SystemExit(0)
'''
    result = subprocess.run([sys.executable, '-c', code, str(lock_path)], capture_output=True)
    held.close()
    assert result.returncode == 42
