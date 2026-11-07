"""NAS dispatch must never substitute Mac services, pointers or theme state."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from local_web_server.app_activation import AppActivator
from local_web_server.cli import main
from local_web_server.fleet_update import FleetUpdater
from local_web_server.fleet_update_models import FleetAppPlan
from local_web_server.models import HostApp, HostRegistry
from local_web_server.nas_models import NasTarget
from local_web_server.nas_transport import NasTransportError


class NasMacosGuardsTests(unittest.TestCase):
    def setUp(self):
        self.registry = HostRegistry('local.example.ts.net', Path('/unavailable/nas-runtime'), (),
            schema_version=3, public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve',
            target=NasTarget('nas-docker', ('/usr/local/bin/adapter',), '172.30.200.0/24', '172.30.200.2'))

    def test_activation_selects_nas_deployer_and_does_not_construct_mac_fallback(self):
        with tempfile.TemporaryDirectory() as text:
            root = Path(text)
            before = {path.name for path in root.iterdir()}
            with patch('local_web_server.app_activation.HostProfilePaths.for_repository'):
                activator = AppActivator(platform_repository=root, registry_path=root/'profile.json',
                                         profile_store=Mock())
            unavailable = NasTransportError('status', 'original', outcome_unknown=True)
            with patch('local_web_server.app_activation.NasHostDeploymentManager') as nas, \
                 patch('local_web_server.app_activation.DeploymentManager') as mac, \
                 patch('local_web_server.deploy.LaunchctlServiceController') as services, \
                 patch('local_web_server.deploy.RuntimeLayout') as layout:
                nas.return_value.deploy.side_effect = unavailable
                selected = activator.deployer_factory(self.registry)
                self.assertIs(selected, nas.return_value)
                with self.assertRaises(NasTransportError): selected.deploy('notes')
            nas.assert_called_once_with(self.registry)
            mac.assert_not_called()
            services.assert_not_called()
            layout.assert_not_called()
            self.assertEqual({path.name for path in root.iterdir()}, before)

    def test_fleet_selects_remote_inspector_without_mac_pointers_or_app_effects(self):
        host = HostApp('notes', Path('/unavailable/notes'), False, None, (), None, None)
        registry = replace(self.registry, apps=(host,))
        update = Mock()
        updater = FleetUpdater(updater_factory=update)
        blocked = FleetAppPlan('notes', 'BLOCKED', None, None, None, None, None, (),
                               'live-release-unavailable', host.repository, None)
        profile = Path('/private/profile.json')
        with patch('local_web_server.fleet_update.load_registry', return_value=registry), \
             patch('local_web_server.fleet_update.NasLiveReleaseInspector') as nas, \
             patch('local_web_server.fleet_update.RuntimeLiveReleaseInspector') as mac, \
             patch('local_web_server.deploy.LaunchctlServiceController') as services, \
             patch.object(updater, '_preview_app', return_value=blocked) as app_preview:
            result = updater.apply(profile)
            self.assertIs(updater._live_inspector, nas.return_value)
        nas.assert_called_once_with(registry)
        mac.assert_not_called()
        services.assert_not_called()
        update.assert_not_called()
        app_preview.assert_called_once_with(host, profile, registry.runtime_root)
        self.assertEqual(result.apps[0].status, 'BLOCKED')

    def test_theme_rollback_refuses_nas_before_local_theme_store(self):
        output, error = io.StringIO(), io.StringIO()
        snapshot = Mock(profile_bytes=b'private fixture', revisions=())
        with patch('local_web_server.cli._load_default_host_profile_snapshot',
                   return_value=(Path('/private/profile.json'), self.registry, snapshot)), \
             patch('local_web_server.theme.ThemeStore') as store, redirect_stdout(output), redirect_stderr(error):
            code = main(['theme', 'rollback'])
        self.assertEqual(code, 2)
        self.assertIn('NAS', error.getvalue())
        self.assertEqual(output.getvalue(), '')
        store.assert_not_called()
