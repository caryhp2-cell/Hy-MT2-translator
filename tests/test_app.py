import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def test_find_model_prefers_translation_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models_dir = Path(temp_dir)
            (models_dir / "A-general-model.gguf").write_text("a", encoding="utf-8")
            (models_dir / "Hy-MT2-1.8B-Q4_K_M.gguf").write_text("hy", encoding="utf-8")

            found = app.find_model(models_dir)

            self.assertEqual(found.name, "Hy-MT2-1.8B-Q4_K_M.gguf")

    def test_find_model_uses_custom_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models_dir = Path(temp_dir)
            preferred = models_dir / "custom.gguf"
            preferred.write_text("custom", encoding="utf-8")
            (models_dir / "Hy-MT2-1.8B-Q4_K_M.gguf").write_text("hy", encoding="utf-8")

            found = app.find_model(models_dir, override_name=str(preferred))

            self.assertEqual(found, preferred)

    def test_build_translation_prompt_requests_concise_professional_english(self):
        chinese = "這是一份測試文件。"
        prompt = app.build_translation_prompt(chinese)

        self.assertIn("English", prompt)
        self.assertIn("without additional explanation", prompt)
        self.assertIn(chinese, prompt)

    def test_clean_translation_removes_thinking_breakdown(self):
        raw = (
            "<think>\nThinking Process:\n1. Analyze the request\n</think>\n\n"
            "English translation: If you want a significantly faster experience, "
            "the most effective approach is to switch to a smaller GGUF model."
        )

        cleaned = app.clean_translation(raw)

        self.assertEqual(
            cleaned,
            "If you want a significantly faster experience, the most effective "
            "approach is to switch to a smaller GGUF model.",
        )

    def test_clean_translation_keeps_plain_translation(self):
        raw = "The weather is nice today, and I want to go out for a walk."

        cleaned = app.clean_translation(raw)

        self.assertEqual(cleaned, raw)


if __name__ == "__main__":
    unittest.main()
