from __future__ import annotations

import unittest
from pathlib import Path


class AppBootTests(unittest.TestCase):
    def test_app_imports_and_templates_compile(self) -> None:
        from app.main import app, templates

        self.assertTrue(app.title)
        template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        names = sorted(path.name for path in template_dir.glob("*.html"))
        self.assertGreaterEqual(len(names), 1)
        for name in names:
            templates.env.get_template(name)
