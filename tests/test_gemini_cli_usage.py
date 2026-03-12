from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import gemini_cli_usage


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def _settings_payload(selected_type: str) -> dict:
    return {"security": {"auth": {"selectedType": selected_type}}}


def _usage_payload(project_root: Path, updated_at: str) -> dict:
    return {
        "project_root": str(project_root.resolve()),
        "auth_type": "oauth-personal",
        "auth_label": "Google login",
        "source": ["quota_api"],
        "updated_at": updated_at,
        "account_quota": {
            "buckets": [],
            "summary_bucket": None,
        },
    }


class GeminiUsageTests(unittest.TestCase):
    def test_env_auth_overrides_workspace_and_global_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            global_settings = tmp_path / "global-settings.json"
            workspace_settings = project_root / ".gemini" / "settings.json"

            _write_json(global_settings, _settings_payload("oauth-personal"))
            _write_json(workspace_settings, _settings_payload("vertex-ai"))

            with (
                mock.patch.object(gemini_cli_usage, "SETTINGS_FILE", global_settings),
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}, clear=True),
            ):
                self.assertEqual(
                    gemini_cli_usage.get_auth_type(project_root), "gemini-api-key"
                )

    def test_workspace_settings_override_global_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            global_settings = tmp_path / "global-settings.json"
            workspace_settings = project_root / ".gemini" / "settings.json"

            _write_json(global_settings, _settings_payload("oauth-personal"))
            _write_json(workspace_settings, _settings_payload("vertex-ai"))

            with (
                mock.patch.object(gemini_cli_usage, "SETTINGS_FILE", global_settings),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(gemini_cli_usage.get_auth_type(project_root), "vertex-ai")

    def test_build_usage_json_is_quota_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            quota = {"buckets": [], "summary_bucket": None}

            with mock.patch.object(gemini_cli_usage, "fetch_quota", return_value=quota):
                usage = gemini_cli_usage.build_usage_json(project_root)

            self.assertEqual(usage["project_root"], str(project_root.resolve()))
            self.assertEqual(usage["source"], ["quota_api"])
            self.assertNotIn("local_usage", usage)
            self.assertEqual(usage["account_quota"], quota)

    def test_summary_bucket_and_statusline_use_highest_used_bucket(self):
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        quota = {
            "buckets": [
                {
                    "model": "gemini-2.5-flash-lite",
                    "used_pct": 0.07,
                    "reset_time": future,
                },
                {
                    "model": "gemini-2.5-pro",
                    "used_pct": 3.5,
                    "reset_time": future,
                },
            ]
        }

        summary = gemini_cli_usage._select_summary_bucket(quota)

        data = {
            "account_quota": {
                "summary_bucket": summary,
            },
        }

        statusline = gemini_cli_usage._statusline_text(data)

        self.assertEqual(summary["model"], "gemini-2.5-pro")
        self.assertIn("q:3.5%", statusline)
        self.assertNotIn("q:0.07%", statusline)

    def test_force_refresh_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            usage_file = tmp_path / "usage-limits.json"
            now = datetime.now(UTC).isoformat()
            cached = _usage_payload(project_root, now)
            fresh = _usage_payload(project_root, "2026-03-12T12:00:00+00:00")
            fresh["auth_type"] = "gemini-api-key"
            fresh["auth_label"] = "Gemini API key"

            usage_file.write_text(json.dumps(cached) + "\n")

            with (
                mock.patch.object(gemini_cli_usage, "DEFAULT_USAGE_FILE", usage_file),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(gemini_cli_usage, "build_usage_json", return_value=fresh) as build_mock,
            ):
                result = gemini_cli_usage._get_cached_usage(project_root=project_root)
                self.assertEqual(result["updated_at"], now)
                build_mock.assert_not_called()

                result = gemini_cli_usage._get_cached_usage(
                    project_root=project_root, force_refresh=True
                )

            self.assertEqual(result, fresh)
            self.assertEqual(json.loads(usage_file.read_text()), fresh)

    def test_refresh_access_token_uses_env_client_credentials(self):
        creds = {
            "refresh_token": "refresh-token",
            "access_token": "old-access-token",
            "expiry_date": 0,
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "access_token": "new-access-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                ).encode()

        def fake_urlopen(request, timeout=10):
            payload = parse_qs(request.data.decode())
            self.assertEqual(payload["client_id"], ["test-client-id"])
            self.assertEqual(payload["client_secret"], ["test-client-secret"])
            self.assertEqual(payload["refresh_token"], ["refresh-token"])
            self.assertEqual(payload["grant_type"], ["refresh_token"])
            return FakeResponse()

        with (
            mock.patch.dict(
                os.environ,
                {
                    "GEMINI_OAUTH_CLIENT_ID": "test-client-id",
                    "GEMINI_OAUTH_CLIENT_SECRET": "test-client-secret",
                },
                clear=True,
            ),
            mock.patch.object(gemini_cli_usage.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            updated = gemini_cli_usage.refresh_access_token(creds)

        self.assertEqual(updated["access_token"], "new-access-token")
        self.assertGreater(updated["expiry_date"], 0)

    def test_refresh_access_token_reads_client_credentials_from_installed_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package_root = tmp_path / "lib" / "node_modules" / "@google" / "gemini-cli"
            oauth2_path = (
                package_root
                / "node_modules"
                / "@google"
                / "gemini-cli-core"
                / "dist"
                / "src"
                / "code_assist"
                / "oauth2.js"
            )
            oauth2_path.parent.mkdir(parents=True, exist_ok=True)
            oauth2_path.write_text(
                "const OAUTH_CLIENT_ID = 'live-client-id';\n"
                "const OAUTH_CLIENT_SECRET = 'live-client-secret';\n"
            )
            gemini_path = package_root / "dist" / "index.js"
            gemini_path.parent.mkdir(parents=True, exist_ok=True)
            gemini_path.write_text("// stub\n")

            creds = {"refresh_token": "refresh-token"}

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps({"access_token": "live-access-token"}).encode()

            def fake_urlopen(request, timeout=10):
                payload = parse_qs(request.data.decode())
                self.assertEqual(payload["client_id"], ["live-client-id"])
                self.assertEqual(payload["client_secret"], ["live-client-secret"])
                return FakeResponse()

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(gemini_cli_usage.shutil, "which", return_value=str(gemini_path)),
                mock.patch.object(gemini_cli_usage.urllib.request, "urlopen", side_effect=fake_urlopen),
            ):
                updated = gemini_cli_usage.refresh_access_token(creds)

        self.assertEqual(updated["access_token"], "live-access-token")

    def test_get_access_token_requires_local_client_metadata_when_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth_file = Path(tmp) / "oauth_creds.json"
            _write_json(
                oauth_file,
                {
                    "access_token": "expired-access-token",
                    "refresh_token": "refresh-token",
                    "expiry_date": 0,
                },
            )

            with (
                mock.patch.object(gemini_cli_usage, "OAUTH_FILE", oauth_file),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(gemini_cli_usage.shutil, "which", return_value=None),
            ):
                with self.assertRaises(RuntimeError) as exc:
                    gemini_cli_usage.get_access_token()

        self.assertIn("GEMINI_OAUTH_CLIENT_ID", str(exc.exception))
        self.assertIn("run `gemini`", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
