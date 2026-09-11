import json
import tempfile
import unittest
from pathlib import Path

from local_web_server.app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    PROVENANCE_FILENAME,
    AppProvenance,
    ProvenanceError,
    UiArtifactReference,
    load_provenance,
    render_provenance,
    validate_compatibility,
)
from local_web_server.models import PlatformSpec


class AppProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / PROVENANCE_FILENAME
        self.payload = {
            "schemaVersion": 1,
            "templateVersion": 1,
            "platformContractVersion": 1,
            "ui": {"version": "0.1.0", "sha256": "a" * 64},
            "capabilities": ["supabase"],
            "domainPaletteTokens": [],
            "managedFiles": [
                "AGENTS.md",
                "local-web.json",
                "vendor/local-web-ui.tgz",
            ],
        }

    def write_payload(self, payload=None):
        selected = self.payload if payload is None else payload
        self.path.write_text(json.dumps(selected), encoding="utf-8")

    def test_uses_exact_generated_filename_and_round_trips_canonical_json(self):
        self.assertEqual(PROVENANCE_FILENAME, ".local-web-platform.json")
        self.write_payload()

        provenance = load_provenance(self.path)

        self.assertEqual(
            provenance,
            AppProvenance(
                schema_version=1,
                template_version=1,
                platform_contract_version=1,
                ui=UiArtifactReference(version="0.1.0", sha256="a" * 64),
                capabilities=("supabase",),
                domain_palette_tokens=(),
                managed_files=(
                    Path("AGENTS.md"),
                    Path("local-web.json"),
                    Path("vendor/local-web-ui.tgz"),
                ),
            ),
        )
        expected = (
            b'{\n'
            b'  "capabilities": [\n'
            b'    "supabase"\n'
            b'  ],\n'
            b'  "domainPaletteTokens": [],\n'
            b'  "managedFiles": [\n'
            b'    "AGENTS.md",\n'
            b'    "local-web.json",\n'
            b'    "vendor/local-web-ui.tgz"\n'
            b'  ],\n'
            b'  "platformContractVersion": 1,\n'
            b'  "schemaVersion": 1,\n'
            b'  "templateVersion": 1,\n'
            b'  "ui": {\n'
            b'    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",\n'
            b'    "version": "0.1.0"\n'
            b'  }\n'
            b'}\n'
        )
        self.assertEqual(render_provenance(provenance), expected)

    def test_current_template_version_is_three_while_version_one_provenance_remains_readable(self):
        self.assertEqual(CURRENT_TEMPLATE_VERSION, 3)
        self.write_payload()

        self.assertEqual(load_provenance(self.path).template_version, 1)

    def test_rejects_unknown_or_missing_keys_at_every_object_boundary(self):
        cases = []
        unknown_root = dict(self.payload, unexpected=True)
        cases.append(unknown_root)
        missing_root = dict(self.payload)
        del missing_root["managedFiles"]
        cases.append(missing_root)
        unknown_ui = dict(self.payload)
        unknown_ui["ui"] = dict(self.payload["ui"], unexpected=True)
        cases.append(unknown_ui)
        missing_ui = dict(self.payload)
        missing_ui["ui"] = {"version": "0.1.0"}
        cases.append(missing_ui)

        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.write_payload(payload)
                with self.assertRaises(ProvenanceError):
                    load_provenance(self.path)

    def test_rejects_noncanonical_collections_and_unsafe_managed_paths(self):
        cases = (
            ("capabilities", ["supabase", "supabase"]),
            ("domainPaletteTokens", ["color-zebra", "color-alpha"]),
            ("domainPaletteTokens", ["color-alpha", "color-alpha"]),
            ("managedFiles", ["local-web.json", "AGENTS.md"]),
            ("managedFiles", ["AGENTS.md", "AGENTS.md"]),
            ("managedFiles", ["../outside"]),
            ("managedFiles", ["/absolute"]),
            ("managedFiles", ["folder\\file"]),
            ("managedFiles", ["."]),
            ("managedFiles", ["bad\x00name"]),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = dict(self.payload)
                payload[field] = value
                self.write_payload(payload)
                with self.assertRaises(ProvenanceError):
                    load_provenance(self.path)

    def test_rejects_capabilities_out_of_lexical_order_before_allowlist_check(self):
        payload = dict(self.payload)
        payload["capabilities"] = ["supabase", "alpha"]
        self.write_payload(payload)

        with self.assertRaisesRegex(
            ProvenanceError,
            "^capabilities must be unique and lexically sorted$",
        ):
            load_provenance(self.path)

    def test_constructor_rejects_mutable_or_noncanonical_container_types(self):
        for field, value in (("version", ["0.1.0"]), ("sha256", ["a" * 64])):
            with self.subTest(model="ui", field=field):
                arguments = {"version": "0.1.0", "sha256": "a" * 64}
                arguments[field] = value
                with self.assertRaises(ProvenanceError):
                    UiArtifactReference(**arguments)

        valid = {
            "schema_version": 1,
            "template_version": 1,
            "platform_contract_version": 1,
            "ui": UiArtifactReference(version="0.1.0", sha256="a" * 64),
            "capabilities": (),
            "domain_palette_tokens": (),
            "managed_files": (),
        }
        invalid_fields = (
            ("ui", {"version": "0.1.0", "sha256": "a" * 64}),
            ("capabilities", []),
            ("capabilities", (1,)),
            ("domain_palette_tokens", []),
            ("domain_palette_tokens", ([],)),
            ("managed_files", []),
            ("managed_files", ("AGENTS.md",)),
        )

        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                arguments = dict(valid)
                arguments[field] = value
                with self.assertRaises(ProvenanceError):
                    AppProvenance(**arguments)

    def test_rejects_invalid_versions_digest_and_scalar_types(self):
        mutations = (
            ("schemaVersion", 2),
            ("schemaVersion", True),
            ("templateVersion", 0),
            ("platformContractVersion", 2),
            ("capabilities", "supabase"),
            ("domainPaletteTokens", ["Color Route"]),
            ("managedFiles", [1]),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                payload = dict(self.payload)
                payload[field] = value
                self.write_payload(payload)
                with self.assertRaises(ProvenanceError):
                    load_provenance(self.path)

        for digest in ("A" * 64, "a" * 63, "g" * 64):
            with self.subTest(digest=digest):
                payload = dict(self.payload)
                payload["ui"] = {"version": "0.1.0", "sha256": digest}
                self.write_payload(payload)
                with self.assertRaises(ProvenanceError):
                    load_provenance(self.path)

        payload = dict(self.payload)
        payload["ui"] = {"version": "v0.1.0", "sha256": "a" * 64}
        self.write_payload(payload)
        with self.assertRaises(ProvenanceError):
            load_provenance(self.path)

    def test_rejects_unsupported_capabilities(self):
        payload = dict(self.payload)
        payload["capabilities"] = ["other"]
        self.write_payload(payload)

        with self.assertRaises(ProvenanceError):
            load_provenance(self.path)

    def test_render_validates_programmatically_constructed_provenance(self):
        invalid = AppProvenance(
            schema_version=1,
            template_version=1,
            platform_contract_version=1,
            ui=UiArtifactReference(version="0.1.0", sha256="A" * 64),
            capabilities=(),
            domain_palette_tokens=(),
            managed_files=(),
        )

        with self.assertRaises(ProvenanceError):
            render_provenance(invalid)

    def test_reports_stable_manifest_compatibility_codes(self):
        self.write_payload()
        provenance = load_provenance(self.path)
        compatible = PlatformSpec(1, 1, "0.1.0", ("supabase",))
        incompatible = PlatformSpec(2, 3, "0.2.0", ())

        self.assertEqual(validate_compatibility(provenance, compatible), ())
        self.assertEqual(
            validate_compatibility(provenance, None),
            ("platform.manifest-missing",),
        )
        self.assertEqual(
            validate_compatibility(provenance, incompatible),
            (
                "platform.contract-version-mismatch",
                "platform.template-version-mismatch",
                "ui.version-mismatch",
                "platform.capabilities-mismatch",
            ),
        )

    def test_parse_and_read_errors_are_sanitised(self):
        private_marker = "private-token-value"
        self.path.write_text(f'{{"secret":"{private_marker}"', encoding="utf-8")

        with self.assertRaises(ProvenanceError) as caught:
            load_provenance(self.path)

        message = str(caught.exception)
        self.assertEqual(message, "cannot read app provenance")
        self.assertNotIn(private_marker, message)
        self.assertNotIn(str(self.path), message)

        missing = self.root / "private-directory" / PROVENANCE_FILENAME
        with self.assertRaises(ProvenanceError) as caught:
            load_provenance(missing)
        self.assertEqual(str(caught.exception), "cannot read app provenance")
        self.assertNotIn(str(missing), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
