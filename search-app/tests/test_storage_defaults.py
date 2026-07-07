from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import patch

from app import object_storage
from app.config import _resolve_storage_backend, _resolve_storage_paths


ROOT = Path(__file__).resolve().parent.parent


class StorageConfigTests(unittest.TestCase):
    def test_python_defaults_use_home_directory(self) -> None:
        home = Path("/home/example-user")
        data_dir, upload_dir, model_dir = _resolve_storage_paths({}, home)
        expected = home / ".oracle-livelabs" / "search-app"
        self.assertEqual(Path(data_dir), expected)
        self.assertEqual(Path(upload_dir), expected / "uploads")
        self.assertEqual(Path(model_dir), expected / "models")

    def test_explicit_paths_remain_authoritative(self) -> None:
        values = {
            "DATA_DIR": "/srv/search-data",
            "UPLOAD_DIR": "/mnt/search-uploads",
            "MODEL_CACHE_DIR": "/var/cache/search-models",
        }
        self.assertEqual(
            _resolve_storage_paths(values, Path("/home/ignored")),
            ("/srv/search-data", "/mnt/search-uploads", "/var/cache/search-models"),
        )

    def test_blank_backend_defaults_to_local_and_invalid_backend_fails(self) -> None:
        self.assertEqual(_resolve_storage_backend({}), "local")
        self.assertEqual(_resolve_storage_backend({"STORAGE_BACKEND": ""}), "local")
        with self.assertRaisesRegex(ValueError, "must be one of"):
            _resolve_storage_backend({"STORAGE_BACKEND": "automatic"})

    def test_local_mode_never_constructs_legacy_oci_store(self) -> None:
        local_settings = SimpleNamespace(
            storage_backend="local",
            object_storage_provider=None,
            oci_os_bucket_name="legacy-bucket",
            s3_bucket_name=None,
        )
        with patch.object(object_storage, "settings", local_settings):
            self.assertIsNone(object_storage.get_object_store("oci"))
            self.assertIsNone(object_storage.default_object_bucket("oci"))

    def test_shell_initializer_is_idempotent_and_local_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="search app home ") as temp_home:
            script = r'''
set -euo pipefail
source ./scripts/storage_env.sh
searchapp_prepare_storage >/dev/null
printf 'keep' > "$UPLOAD_DIR/existing.txt"
searchapp_prepare_storage >/dev/null
test "$(cat "$UPLOAD_DIR/existing.txt")" = keep
printf '%s\n%s\n%s\n%s\n' "$STORAGE_BACKEND" "$DATA_DIR" "$UPLOAD_DIR" "$MODEL_CACHE_DIR"
'''
            env = os.environ.copy()
            for name in (
                "DATA_DIR",
                "UPLOAD_DIR",
                "MODEL_CACHE_DIR",
                "SEARCHAPP_LOG_DIR",
                "SEARCHAPP_RUN_DIR",
                "SEARCHAPP_RUNTIME_DIR",
                "STORAGE_BACKEND",
            ):
                env.pop(name, None)
            env["HOME"] = temp_home
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            backend, data_dir, upload_dir, model_dir = result.stdout.strip().splitlines()
            expected = Path(temp_home) / ".oracle-livelabs" / "search-app"
            self.assertEqual(backend, "local")
            self.assertEqual(Path(data_dir), expected)
            self.assertEqual(Path(upload_dir), expected / "uploads")
            self.assertEqual(Path(model_dir), expected / "models")

    def test_oci_storage_requires_explicit_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temp_home:
            env = os.environ.copy()
            env.update({"HOME": temp_home, "STORAGE_BACKEND": "oci"})
            env.pop("OCI_OS_BUCKET_NAME", None)
            result = subprocess.run(
                ["bash", "-c", "source ./scripts/storage_env.sh; searchapp_prepare_storage"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires an explicitly configured OCI_OS_BUCKET_NAME", result.stderr)


if __name__ == "__main__":
    unittest.main()
