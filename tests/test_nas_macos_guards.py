"""Unsupported NAS lifecycle routes must stop before local effects."""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from local_web_server.app_activation import AppActivator, AppActivationError
from local_web_server.cli import main
from local_web_server.fleet_update import FleetUpdater
from local_web_server.models import HostRegistry
from local_web_server.nas_models import NasTarget


class NasMacosGuardsTests(unittest.TestCase):
    def setUp(self):
        self.registry = HostRegistry('local.example.ts.net', Path('/unavailable/nas-runtime'), (),
            schema_version=3, public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve',
            target=NasTarget('nas-docker', ('/usr/local/bin/adapter',), '172.30.200.0/24', '172.30.200.2'))

    def test_activation_stops_before_source_inspection_profile_or_service_effects(self):
        activator = object.__new__(AppActivator)
        activator.registry_path = Path('/private/profile.json')
        activator.platform_repository = Path('/private/framework')
        activator.profile_store = Mock()
        activator.registrar = Mock()
        activator.install = Mock()
        with patch('local_web_server.app_activation.load_registry', return_value=self.registry), \
             patch('local_web_server.app_activation.require_committed_installation_sources') as inspect:
            with self.assertRaisesRegex(AppActivationError, 'NAS'):
                activator.preview(Path('/missing/app'))
        inspect.assert_not_called()
        self.assertEqual(activator.profile_store.mock_calls, [])
        self.assertEqual(activator.registrar.mock_calls, [])
        activator.install.assert_not_called()

    def test_fleet_stops_before_local_pointer_inspection_or_app_updates(self):
        updater = FleetUpdater()
        with patch('local_web_server.fleet_update.load_registry', return_value=self.registry), \
             patch.object(updater, '_preview_app') as app_preview:
            with self.assertRaisesRegex(ValueError, 'NAS'):
                updater.apply(Path('/private/profile.json'))
        app_preview.assert_not_called()

    def test_theme_rollback_refuses_nas_before_local_theme_store(self):
        output, error = io.StringIO(), io.StringIO()
        with patch('local_web_server.cli._load_default_host_profile', return_value=(Path('/private/profile.json'), self.registry)), \
             patch('local_web_server.theme.ThemeStore') as store, redirect_stdout(output), redirect_stderr(error):
            code = main(['theme', 'rollback'])
        self.assertEqual(code, 2)
        self.assertIn('NAS', error.getvalue())
        self.assertEqual(output.getvalue(), '')
        store.assert_not_called()
