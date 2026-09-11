import unittest
from pathlib import Path

from local_web_server.service_command import (
    ServiceCommandTemplateError,
    expand_service_command_template,
    validate_service_command_template,
)


class ServiceCommandTemplateTests(unittest.TestCase):
    def test_accepts_and_expands_the_fixed_placeholder_vocabulary(self):
        repository = Path("/private/Repository With Spaces")
        release = Path("/private/runtime/apps/notes/current")
        argv = (
            "/usr/bin/env", "node", "{release}/server.mjs",
            "--port={port}", "--data-dir", "{repository}/data",
        )

        validate_service_command_template(argv)

        self.assertEqual(
            expand_service_command_template(
                argv, port=52991, release=release, repository=repository
            ),
            (
                "/usr/bin/env", "node", str(release / "server.mjs"),
                "--port=52991", "--data-dir", str(repository / "data"),
            ),
        )

    def test_rejects_unknown_malformed_unresolved_and_missing_port_templates(self):
        cases = (
            (("node", "{private}", "{port}"), "unsupported placeholder"),
            (("node", "{{port}}"), "unsupported placeholder"),
            (("node", "path/{repository", "{port}"), "unsupported placeholder"),
            (("node", "{repository}/data"), "must contain {port}"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ServiceCommandTemplateError, message):
                    validate_service_command_template(argv)
