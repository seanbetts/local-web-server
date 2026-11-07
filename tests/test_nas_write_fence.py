"""Cutover write fences, exercised through a disposable native Caddy listener."""
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from local_web_server.models import AppManifest, BuildSpec, HostApp, HostRegistry, ServiceSpec
from local_web_server.nas_models import NasTarget
from local_web_server.render import render_caddyfile
from tests.helpers import TEST_CADDY, caddy_integration


class NasWriteFenceTests(unittest.TestCase):
    @caddy_integration
    def test_readonly_cutover_blocks_writes_before_backend_and_explicit_enable_restores_them(self):
        calls=[]
        class Backend(BaseHTTPRequestHandler):
            def serve(self):
                calls.append(self.command)
                self.send_response(204)
                self.send_header('X-Nas-Fence-Fixture','ready')
                self.end_headers()
            do_GET=do_HEAD=do_OPTIONS=do_POST=do_PUT=do_PATCH=do_DELETE=serve
            def log_message(self,*args): pass
        backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        thread=threading.Thread(target=backend.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                upstream=backend.server_address[1]
                manifest=AppManifest(1,'notes','Notes','/notes','service',BuildSpec((),Path('release'),()),
                                     '/notes/healthz',ServiceSpec('server','/healthz'))
                registry=HostRegistry('local.example.ts.net',root/'remote-unused',
                    (HostApp('notes',root/'source-unused',False,None,(),upstream,None),),3,
                    'https://local.example.ts.net','tailscale-serve',
                    NasTarget('nas-docker',('/fixture/adapter',),'172.30.200.0/24','172.30.200.2'))
                for enabled in (False,True):
                    with self.subTest(writes_enabled=enabled),socket.socket() as reservation:
                        reservation.bind(('127.0.0.1',0))
                        port=reservation.getsockname()[1]
                    target=replace(registry.target,writes_enabled=enabled)
                    config=render_caddyfile(replace(registry,target=target),{'notes':manifest})
                    # Only fixture network addressing changes; NAS route ordering
                    # and policy are the production renderer's exact output.
                    config=config.replace(':8080',':'+str(port)).replace(f'app-notes:{upstream}',f'127.0.0.1:{upstream}')
                    config=config.replace('    servers {','    admin off\n    servers {',1)
                    path=root/'Caddyfile';path.write_text(config)
                    with tempfile.TemporaryFile() as log:
                        process=subprocess.Popen([str(TEST_CADDY),'run','--config',str(path),'--adapter','caddyfile'],
                            stdout=subprocess.DEVNULL,stderr=log,
                            env=dict(PATH=os.defpath,HOME=str(root),XDG_CONFIG_HOME=str(root/'config'),XDG_DATA_HOME=str(root/'data')))
                        def request(method):
                            connection=HTTPConnection('127.0.0.1',port,timeout=1)
                            try:
                                connection.request(method,'/notes/api/fixture',headers={'Host':registry.host})
                                response=connection.getresponse();response.read()
                                return response.status,response.getheader('X-Nas-Fence-Fixture'),response.getheader('Cache-Control')
                            finally: connection.close()
                        try:
                            deadline=time.monotonic()+5
                            while True:
                                try:
                                    if request('HEAD')[:2]==(204,'ready'): break
                                except OSError: pass
                                if process.poll() is not None or time.monotonic()>deadline:
                                    log.seek(0);self.fail('isolated Caddy readiness failed: '+log.read(4096).decode())
                                time.sleep(.02)
                            calls.clear()
                            for method in ('GET','HEAD','OPTIONS'):
                                self.assertEqual(request(method)[:2],(204,'ready'))
                            for method in ('POST','PUT','PATCH','DELETE'):
                                status,marker,cache=request(method)
                                self.assertEqual(status,204 if enabled else 503)
                                if not enabled:
                                    self.assertIsNone(marker)
                                    self.assertEqual(cache,'no-store')
                            self.assertEqual(calls,['GET','HEAD','OPTIONS']+(['POST','PUT','PATCH','DELETE'] if enabled else []))
                        finally:
                            process.terminate()
                            try: process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.kill();process.wait(timeout=3)
                self.assertFalse(registry.runtime_root.exists())
        finally:
            backend.shutdown();backend.server_close();thread.join(timeout=3)
