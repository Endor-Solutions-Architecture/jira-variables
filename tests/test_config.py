"""Tests for .env loading."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jira_variables_proxy.config import Config, parse_dotenv


class ParseDotenvTest(unittest.TestCase):
    def test_quotes_comments_and_export(self) -> None:
        values = parse_dotenv(
            '\n'.join(
                [
                    "# comment",
                    "PUBLIC_BASE_URL=https://shim.example",
                    "export ENDOR_API_CREDENTIALS_SECRET=\"s e c'ret\"",
                    "ENDOR_API_CREDENTIALS_KEY='key'",
                    "",
                ]
            )
        )
        self.assertEqual(values["PUBLIC_BASE_URL"], "https://shim.example")
        self.assertEqual(values["ENDOR_API_CREDENTIALS_SECRET"], "s e c'ret")
        self.assertEqual(values["ENDOR_API_CREDENTIALS_KEY"], "key")

    def test_invalid_line_names_the_line_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_dotenv("OK=1\nnot a pair\n")


class FromDotenvTest(unittest.TestCase):
    def test_missing_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "required"):
            Config.from_dotenv("/tmp/jira-variables-proxy-missing.env")

    def test_missing_secret_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "PUBLIC_BASE_URL=https://shim.example",
                        "JIRA_BASE_URL=https://jira.example",
                        "ENDOR_API_URL=https://api.endorlabs.com",
                        "ENDOR_API_CREDENTIALS_KEY=key",
                        "ENDOR_API_CREDENTIALS_SECRET=",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ENDOR_API_CREDENTIALS_SECRET is required"):
                Config.from_dotenv(path)

    def test_loads_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "PUBLIC_BASE_URL=https://shim.example/",
                        "JIRA_BASE_URL=https://jira.example/",
                        "ENDOR_API_URL=https://api.endorlabs.com/",
                        "ENDOR_API_CREDENTIALS_KEY=key",
                        "ENDOR_API_CREDENTIALS_SECRET=secret",
                        "LISTEN_ADDR=127.0.0.1:9090",
                    ]
                ),
                encoding="utf-8",
            )
            config = Config.from_dotenv(path)
        self.assertEqual(config.public_base_url, "https://shim.example")
        self.assertEqual(config.endor_api_key, "key")
        self.assertEqual(config.endor_api_secret, "secret")
        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.listen_port, 9090)


if __name__ == "__main__":
    unittest.main()
