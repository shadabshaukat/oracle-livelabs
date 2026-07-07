from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent


class MacOllamaShellPolicyTests(unittest.TestCase):
    def test_explicit_existing_cli_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cli = Path(temp_dir) / "custom ollama"
            cli.write_text("#!/usr/bin/env bash\nexit 0\n")
            cli.chmod(0o755)
            env = os.environ.copy()
            env["OLLAMA_CLI_PATH"] = str(cli)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "source ./scripts/macos_ollama.sh; searchapp_macos_find_ollama_cli /missing/managed/ollama",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), str(cli))

    def test_starting_external_cli_preserves_its_model_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "environment.txt"
            pid_file = root / "ollama.pid"
            log_file = root / "ollama.log"
            cli = root / "ollama"
            cli.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"${OLLAMA_MODELS-unset}\" > \"$TEST_OUTPUT\"\n"
            )
            cli.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "TEST_OUTPUT": str(output),
                    "OLLAMA_NUM_CTX": "8192",
                    "OLLAMA_KEEP_ALIVE": "-1",
                }
            )
            env.pop("OLLAMA_MODELS", None)
            subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        "source ./scripts/macos_ollama.sh; "
                        f"searchapp_macos_start_ollama '{cli}' /different/managed/ollama "
                        f"'{root / 'managed-models'}' '{pid_file}' '{log_file}'"
                    ),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            deadline = time.monotonic() + 2
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(output.read_text().strip(), "unset")


if __name__ == "__main__":
    unittest.main()
