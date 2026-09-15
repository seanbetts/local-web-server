"""One real Caddy journey owns hosted assets, recovery, retry and data retention."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_public_base_path_migration as migration
from tests.suites import acceptance


class PublicBasePathMigrationWorkflowTests(unittest.TestCase):
    @acceptance
    def test_real_migration_recovers_retries_and_preserves_external_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / "workflows"
            workflows.mkdir()
            external = root / "state.json"
            external.write_bytes(migration._DATA_SENTINEL)
            external.chmod(0o640)
            before = migration._file_state(external)
            output = []
            verifier = migration.PublicBasePathMigrationWorkflowVerifier(
                coding_root=workflows,
                external_data=external,
                emit=output.append,
            )
            with patch.dict(
                os.environ,
                {
                    migration._PARENT_SECRET_ENVIRONMENT_NAME: "must-not-reach-build-or-service"
                },
            ):
                self.assertEqual(verifier.run(), 0, "\n".join(output))
            self.assertEqual(migration._file_state(external), before)
            self.assertEqual(list(workflows.iterdir()), [])
