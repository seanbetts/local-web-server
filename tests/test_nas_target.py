import json
from pathlib import Path
import tempfile
import unittest

from local_web_server.config import ConfigError, load_registry
from local_web_server.deploy import DeploymentManager
from local_web_server.install import Installer
from local_web_server.status import StatusCollector


class NasTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.payload=dict(schemaVersion=3,publicOrigin='https://local.example.ts.net',
                          ingressMode='tailscale-serve',runtimeRoot=str(self.root/'runtime'),
                          apps=[],target=dict(kind='macos'),services=[])

    def tearDown(self):self.temp.cleanup()

    def load(self):
        path=self.root/'profile.json';path.write_text(json.dumps(self.payload));return load_registry(path)

    def test_schema3_macos_preserves_existing_origin(self):
        registry=self.load()
        self.assertEqual(registry.public_origin,self.payload['publicOrigin'])
        self.assertEqual(registry.target.kind,'macos')
        self.assertEqual(registry.services,())

    def test_nas_target_accepts_private_network_and_adapter(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
                                    networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        registry=self.load()
        self.assertEqual(registry.target.adapter_command,('/usr/local/bin/local-web-nas',))
        self.assertEqual(registry.target.proxy_address,'172.30.200.2')

    def test_nas_writes_default_to_fenced_and_require_an_explicit_boolean(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
                                    networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        self.assertFalse(self.load().target.writes_enabled)
        self.payload['target']['writesEnabled']=True
        self.assertTrue(self.load().target.writes_enabled)
        for value in (None, 'false', 0, 1):
            self.payload['target']['writesEnabled']=value
            with self.subTest(value=value),self.assertRaises(ConfigError): self.load()

    def test_nas_target_declares_a_named_mac_builder_without_using_ambient_docker(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
            networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2',buildContext='local-web-build')
        self.assertEqual(self.load().target.build_context, 'local-web-build')
        for value in ('', '-host', 'two words', ['context']):
            self.payload['target']['buildContext'] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.load()

    def test_nas_platform_release_identity_cannot_be_registered_as_a_user_app(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
            networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        self.payload['apps']=[dict(id='local-web-platform',repository=str(self.root/'reserved'),autoDeploy=False)]
        with self.assertRaisesRegex(ConfigError, 'reserved'):
            self.load()

    def test_nas_target_cannot_fall_through_to_macos_operational_managers(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
                                    networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        registry=self.load()
        for constructor in (lambda:DeploymentManager(registry), lambda:StatusCollector(registry),
                            lambda:Installer(registry, {}, repository=self.root)):
            with self.subTest(constructor=constructor),self.assertRaisesRegex(ValueError,'NAS'):
                constructor()

    def test_nas_target_rejects_lan_fallback(self):
        self.payload.pop('publicOrigin');self.payload.update(host='example.local',ingressMode='trusted-lan')
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/local-web-nas'],
                                    networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        with self.assertRaises(ConfigError):self.load()

    def test_schema3_macos_can_keep_trusted_lan_mode(self):
        self.payload.pop('publicOrigin');self.payload.update(host='example.local',ingressMode='trusted-lan')
        self.assertEqual(self.load().public_origin,'http://example.local')

    def test_adapter_and_network_fields_are_not_accepted_for_macos(self):
        self.payload['target']['adapterCommand']=['/usr/local/bin/adapter']
        with self.assertRaises(ConfigError):self.load()

    def test_invalid_network_or_proxy_is_rejected(self):
        self.payload['target']=dict(kind='nas-docker',adapterCommand=['/usr/local/bin/adapter'],
                                    networkSubnet='172.30.200.0/24',proxyAddress='172.30.200.2')
        for key,bad in [('proxyAddress','172.31.0.2'),('proxyAddress','172.30.200.0'),
                        ('proxyAddress','172.30.200.255'),('networkSubnet','0.0.0.0/0'),
                        ('networkSubnet','172.30.200.3/24'),('adapterCommand',['relative'])]:
            original=self.payload['target'][key];self.payload['target'][key]=bad
            with self.subTest(key=key,bad=bad),self.assertRaises(ConfigError):self.load()
            self.payload['target'][key]=original

    def test_services_are_links_not_managed_apps(self):
        self.payload['services']=[dict(id='jellyfin',title='Jellyfin',url='https://local.example.ts.net:8444',
                                       icon='video',accent='#3859D6')]
        registry=self.load()
        self.assertEqual(registry.apps,())
        self.assertEqual(registry.services[0].url,'https://local.example.ts.net:8444')

    def test_duplicate_or_unsafe_service_links_are_rejected(self):
        link=dict(id='jellyfin',title='Jellyfin',url='https://local.example.ts.net:8444',icon='video',accent='#3859D6')
        for url in ['javascript:alert(1)','http://example.com','https://user:password@example.com',
                    'https://example.com/?token=secret','https://example.com/\n','https://example.com/\\evil',
                    'https://example.com/?', 'https://example.com/#', 'https://example.com/\u0080',
                    'https://example.123', 'https://0x7f.1', 'https://999.999.999.999',
                    'https://example.com:', 'https://example.com:08443', 'https://xn--a', 'https://xn--abc',
                    'https://xn--bcher-kva.example']:
            self.payload['services']=[dict(link,url=url)]
            with self.subTest(url=url),self.assertRaises(ConfigError):self.load()
        self.payload['services']=[link,link]
        with self.assertRaises(ConfigError):self.load()

    def test_old_schemas_default_to_macos_and_empty_services(self):
        original = dict(self.payload)
        for version in (1,2):
            value=dict(original);value.pop('target');value.pop('services');value['schemaVersion']=version
            if version==1:
                value.pop('publicOrigin');value.pop('ingressMode');value['host']='example.local'
            self.payload=value
            registry=self.load();self.assertEqual(registry.target.kind,'macos');self.assertEqual(registry.services,())
