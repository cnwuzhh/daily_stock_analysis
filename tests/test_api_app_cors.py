# -*- coding: utf-8 -*-
"""Tests for FastAPI app CORS configuration."""

import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.middleware.cors import CORSMiddleware

from api.app import create_app


class AppCorsConfigTestCase(unittest.TestCase):
    """CORS configuration should stay browser-compatible."""

    def _build_app(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return create_app(static_dir=Path(temp_dir.name))

    def test_allow_all_disables_credentials(self):
        with patch.dict(os.environ, {"CORS_ALLOW_ALL": "true"}, clear=False):
            app = self._build_app()

        cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        self.assertEqual(cors.kwargs["allow_origins"], ["*"])
        self.assertFalse(cors.kwargs["allow_credentials"])

    def test_explicit_origin_list_keeps_credentials_enabled(self):
        with patch.dict(os.environ, {"CORS_ALLOW_ALL": "false"}, clear=False):
            app = self._build_app()

        cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        self.assertIn("http://localhost:5173", cors.kwargs["allow_origins"])
        self.assertTrue(cors.kwargs["allow_credentials"])

    def test_lifespan_initializes_database_manager(self):
        async def _run_lifespan():
            with patch("api.app.DatabaseManager.get_instance", return_value=object()) as mock_db_get:
                app = self._build_app()
                async with app.router.lifespan_context(app):
                    self.assertTrue(hasattr(app.state, "db_manager"))
                    self.assertTrue(hasattr(app.state, "system_config_service"))
                self.assertFalse(hasattr(app.state, "db_manager"))
                self.assertFalse(hasattr(app.state, "system_config_service"))
                mock_db_get.assert_called_once()

        asyncio.run(_run_lifespan())


if __name__ == "__main__":
    unittest.main()
