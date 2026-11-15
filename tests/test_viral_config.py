import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.viral.config import load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_are_built_from_the_project_root(self):
        config = load_config(env={}, project_root=self.root)
        self.assertEqual(config.output_dir, (self.root / "Clipping" / "output" / "viral").resolve())
        self.assertEqual(config.temp_dir, (self.root / "Clipping" / "tmp").resolve())
        self.assertEqual(config.cache_dir, config.output_dir / ".cache")
        self.assertEqual(config.download_format, "bv*+ba/b")
        self.assertEqual(config.llm_provider, "auto")
        self.assertEqual(config.transcription_provider, "auto")
        self.assertEqual(config.prompt_file, (self.root / "prompts" / "viral_clip_analysis.txt").resolve())

    def test_environment_values_are_parsed_and_relative_paths_resolve(self):
        config = load_config(
            env={
                "VIRAL_OUTPUT_DIR": "custom/out",
                "VIRAL_TEMP_DIR": "custom/tmp",
                "VIRAL_CACHE_DIR": "custom/cache",
                "MAX_CLIPS": "4",
                "MAX_RETRIES": "5",
                "MIN_CLIP_DURATION": "12.5",
                "MAX_CLIP_DURATION": "45",
                "LLM_JSON_MODE": "false",
                "KEEP_AUDIO": "yes",
                "CLIP_CRF": "21",
                "LLM_MAX_TRANSCRIPT_CHARS": "1000",
            },
            project_root=self.root,
        )
        self.assertEqual(config.output_dir, (self.root / "custom" / "out").resolve())
        self.assertEqual(config.temp_dir, (self.root / "custom" / "tmp").resolve())
        self.assertEqual(config.cache_dir, (self.root / "custom" / "cache").resolve())
        self.assertEqual(config.max_clips, 4)
        self.assertEqual(config.max_retries, 5)
        self.assertEqual(config.default_min_clip_duration, 12.5)
        self.assertEqual(config.default_max_clip_duration, 45)
        self.assertFalse(config.llm_json_mode)
        self.assertTrue(config.keep_audio)
        self.assertEqual(config.clip_crf, 21)
        # The prompt budget has a floor so a tiny value cannot break the prompt.
        self.assertEqual(config.llm_max_transcript_chars, 4000)

    def test_invalid_numbers_and_reversed_ranges_fall_back_safely(self):
        config = load_config(
            env={
                "MAX_CLIPS": "not-a-number",
                "MAX_RETRIES": "0",
                "MIN_CLIP_DURATION": "80",
                "MAX_CLIP_DURATION": "20",
                "JOB_WORKERS": "-2",
            },
            project_root=self.root,
        )
        self.assertEqual(config.max_clips, 10)
        self.assertEqual(config.max_retries, 1)
        self.assertEqual(config.job_workers, 1)
        self.assertEqual(config.default_max_clip_duration, 80)

    def test_transcription_key_falls_back_to_the_llm_key(self):
        config = load_config(
            env={"LLM_API_KEY": "sk-test-value", "LLM_BASE_URL": "https://example.invalid/v1"},
            project_root=self.root,
        )
        self.assertEqual(config.transcription_api_key, "sk-test-value")
        self.assertEqual(config.transcription_base_url, "https://example.invalid/v1")

    def test_gemini_provider_uses_google_defaults(self):
        config = load_config(
            env={
                "LLM_PROVIDER": "google",
                "LLM_API_KEY": "key",
                "LLM_MODEL": "gpt-4o-mini",
                "LLM_BASE_URL": "https://api.openai.com/v1",
            },
            project_root=self.root,
        )
        self.assertEqual(config.llm_provider, "gemini")
        self.assertEqual(config.llm_base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(config.llm_model, "gemini-3.8-flash")

    def test_gemini_provider_sets_defaults_when_unset(self):
        config = load_config(env={"LLM_PROVIDER": "gemini", "LLM_API_KEY": "key"}, project_root=self.root)
        self.assertEqual(config.llm_base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(config.llm_model, "gemini-3.8-flash")

    def test_gemini_provider_respects_explicit_model_and_base_url(self):
        config = load_config(
            env={
                "LLM_PROVIDER": "google_gemini",
                "LLM_BASE_URL": "https://proxy.invalid/v1",
                "LLM_MODEL": "gemini-custom",
            },
            project_root=self.root,
        )
        self.assertEqual(config.llm_provider, "gemini")
        self.assertEqual(config.llm_base_url, "https://proxy.invalid/v1")
        self.assertEqual(config.llm_model, "gemini-custom")

    def test_openai_defaults_are_unchanged(self):
        config = load_config(env={"LLM_PROVIDER": "auto"}, project_root=self.root)
        self.assertEqual(config.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(config.llm_model, "gpt-4o-mini")

    def test_dotenv_is_loaded_but_process_environment_wins(self):
        (self.root / ".env").write_text(
            "LLM_MODEL=from-dotenv\nLLM_API_KEY=dotenv-key\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"LLM_MODEL": "from-process"}, clear=True):
            config = load_config(project_root=self.root)
        self.assertEqual(config.llm_model, "from-process")
        self.assertEqual(config.llm_api_key, "dotenv-key")

    def test_public_dict_never_contains_secrets(self):
        config = load_config(
            env={
                "LLM_API_KEY": "sk-secret",
                "TRANSCRIPTION_API_KEY": "sk-other",
                "VIRAL_API_TOKEN": "api-secret",
            },
            project_root=self.root,
        )
        public = config.public_dict()
        text = repr(public)
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("sk-other", text)
        self.assertNotIn("api-secret", text)


if __name__ == "__main__":
    unittest.main()
