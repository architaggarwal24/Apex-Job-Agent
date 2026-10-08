"""
tests/test_providers.py — custom OpenAI-compatible providers, user-added model
names, placeholder-key repair, and what llm_config() resolves to.

Run from the project root:   python3 -m unittest discover -s tests -v
Uses a throwaway settings DB and a throwaway encryption key; never touches data/.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

_ENV_KEYS = ["MISTRAL_API_KEY", "NVIDIA_NIM_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
             "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER", "MISTRAL_MODEL", "NVIDIA_NIM_MODEL",
             "OPENROUTER_MODEL", "GEMINI_MODEL", "OLLAMA_MODEL", "OLLAMA_BASE_URL", "ANTHROPIC_MODEL",
             "OPENAI_MODEL", "LLM_TEMPERATURE"]


class ProviderTestBase(unittest.TestCase):
    extra_env: dict = {}

    def setUp(self):
        from core import crypto
        from db.settings_db import SettingsDB
        env = {k: "" for k in _ENV_KEYS}
        env["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        env.update(self.extra_env)
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self._old_fernet = crypto._fernet
        crypto._fernet = None
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.db"
        self.db = SettingsDB(path=self.path)

    def tearDown(self):
        from core import crypto
        crypto._fernet = self._old_fernet
        self._env.stop()
        self.tmp.cleanup()

    def raw(self, key):
        with self.db._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


class PlaceholderKeyTests(ProviderTestBase):
    extra_env = {"MISTRAL_API_KEY": "your_mistral_key_here",       # what .env.example ships
                 "GEMINI_API_KEY": "your_gemini_key_here",
                 "OPENAI_API_KEY": "sk-proj-RealLookingKey123"}

    def test_env_placeholders_are_not_imported_but_real_keys_are(self):
        self.assertEqual(self.db.read("mistral_api_key"), "")
        self.assertEqual(self.db.read("gemini_api_key"), "")
        self.assertEqual(self.db.read("openai_api_key"), "sk-proj-RealLookingKey123")
        self.assertTrue(self.raw("openai_api_key").startswith("enc:"))

    def test_providers_with_only_a_placeholder_are_not_reported_configured(self):
        self.assertFalse(self.db.is_configured("mistral"))
        self.assertFalse(self.db.is_configured("gemini"))
        self.assertTrue(self.db.is_configured("openai"))
        self.assertTrue(self.db.is_configured("ollama"))        # needs no key

    def test_placeholder_already_stored_by_an_older_version_is_repaired_on_startup(self):
        from db.settings_db import SettingsDB
        self.db.write("anthropic_api_key", "your_anthropic_key_here")   # what the old migration produced
        self.assertTrue(self.raw("anthropic_api_key").startswith("enc:"))
        again = SettingsDB(path=self.path)                                # app restart
        self.assertEqual(again.read("anthropic_api_key"), "")
        self.assertFalse(again.is_configured("anthropic"))
        self.assertEqual(again.read("openai_api_key"), "sk-proj-RealLookingKey123")   # real key untouched

    def test_llm_config_never_hands_a_placeholder_to_an_sdk(self):
        self.db.write("llm_provider", "gemini")
        self.db.write("gemini_api_key", "your_gemini_key_here")
        self.assertEqual(self.db.llm_config()["api_key"], "")


class CustomProviderTests(ProviderTestBase):
    def add(self, **kw):
        args = dict(label="CodeCraft API", base_url="https://codecraftapi.com/v1",
                    api_key="cc_secret123", model="claude-opus-4.8")
        args.update(kw)
        return self.db.add_custom_provider(**args)

    def test_add_then_use_it_as_the_active_provider(self):
        p = self.add()
        self.assertEqual(p["id"], "custom-codecraft-api")
        self.db.write("llm_provider", p["id"])
        c = self.db.llm_config()
        self.assertEqual((c["type"], c["base_url"], c["model"], c["api_key"], c["provider"], c["label"]),
                         ("openai_compat", "https://codecraftapi.com/v1", "claude-opus-4.8",
                          "cc_secret123", "custom-codecraft-api", "CodeCraft API"))
        self.assertEqual(self.db.provider_info(),
                         {"provider": "custom-codecraft-api", "model": "claude-opus-4.8", "label": "CodeCraft API"})

    def test_api_key_is_encrypted_at_rest(self):
        p = self.add()
        raw = self.raw("custom_codecraft-api_api_key")
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("cc_secret123", raw)
        self.assertNotIn("cc_secret123", self.raw("custom_providers"))     # not in the JSON list either

    def test_ids_are_unique(self):
        a, b, c = self.add(), self.add(), self.add()
        self.assertEqual([a["id"], b["id"], c["id"]],
                         ["custom-codecraft-api", "custom-codecraft-api-2", "custom-codecraft-api-3"])
        self.assertEqual(len(self.db.list_custom_providers()), 3)

    def test_unsafe_or_invalid_input_is_rejected_and_writes_nothing(self):
        bad = [dict(base_url="http://169.254.169.254/latest"), dict(base_url="http://2852039166/"),
               dict(base_url="file:///etc/passwd"), dict(base_url="https://u:p@x.com/v1"),
               dict(base_url=""), dict(label=""), dict(label="x" * 41), dict(label="<script>"),
               dict(model="bad model name"), dict(models=["ok-model", "not ok!"])]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                self.add(**kw)
        self.assertEqual(self.db.list_custom_providers(), [], "a failed add left a half-created provider")
        self.assertIsNone(self.raw("custom_codecraft-api_api_key"))

    def test_keyless_local_server_gets_a_dummy_key_the_sdk_accepts(self):
        p = self.add(label="LM Studio", base_url="http://localhost:1234/v1", api_key="", model="qwen3")
        self.db.write("llm_provider", p["id"])
        self.assertEqual(self.db.llm_config()["api_key"], "not-needed")
        self.assertTrue(self.db.is_configured(p["id"]))

    def test_remote_provider_without_a_key_is_not_configured(self):
        p = self.add(api_key="")
        self.assertFalse(self.db.is_configured(p["id"]))

    def test_models_passed_at_creation_are_saved(self):
        p = self.add(models=["gpt-x", "claude-y"], model="claude-opus-4.8")
        self.assertEqual(self.db.extra_models(p["id"]), ["gpt-x", "claude-y", "claude-opus-4.8"])

    def test_update_validates_and_persists(self):
        p = self.add()
        self.db.update_custom_provider(p["id"], label="CC", base_url="https://codecraftapi.com/v1/")
        got = self.db.list_custom_providers()[0]
        self.assertEqual((got["label"], got["base_url"]), ("CC", "https://codecraftapi.com/v1"))
        with self.assertRaises(ValueError):
            self.db.update_custom_provider(p["id"], base_url="http://169.254.169.254/")
        with self.assertRaises(LookupError):
            self.db.update_custom_provider("custom-nope", label="x")
        self.assertEqual(self.db.list_custom_providers()[0]["base_url"], "https://codecraftapi.com/v1")

    def test_delete_removes_everything_and_resets_active_provider(self):
        p = self.add()
        self.db.write("llm_provider", p["id"])
        self.db.delete_custom_provider(p["id"])
        self.assertEqual(self.db.list_custom_providers(), [])
        for key in ("custom_codecraft-api_api_key", "custom_codecraft-api_model", f"extra_models_{p['id']}"):
            self.assertIsNone(self.raw(key), key)
        self.assertEqual(self.db.read("llm_provider"), "mistral")
        self.assertEqual(self.db.llm_config()["provider"], "mistral")
        with self.assertRaises(LookupError):
            self.db.delete_custom_provider(p["id"])

    def test_active_provider_that_no_longer_exists_falls_back_honestly(self):
        self.db.write("llm_provider", "custom-ghost")
        c = self.db.llm_config()
        self.assertEqual(c["provider"], "mistral")             # label matches the config actually used
        self.db.write("llm_provider", "totally-unknown")
        self.assertEqual(self.db.llm_config()["provider"], "mistral")

    def test_provider_count_is_capped(self):
        import db.settings_db as m
        old, m.MAX_CUSTOM_PROVIDERS = m.MAX_CUSTOM_PROVIDERS, 3
        try:
            for i in range(3):
                self.add(label=f"P{i}")
            with self.assertRaises(ValueError):
                self.add(label="one too many")
        finally:
            m.MAX_CUSTOM_PROVIDERS = old

    def test_corrupt_provider_json_is_survivable(self):
        self.db.write("custom_providers", "{not json")
        self.assertEqual(self.db.list_custom_providers(), [])
        self.db.write("custom_providers", json.dumps([{"id": "custom-a"}, "junk", {"id": "x", "label": "l", "base_url": "u"}]))
        self.assertEqual(self.db.list_custom_providers(), [])


class ExtraModelTests(ProviderTestBase):
    def test_add_dedupe_remove_for_a_builtin_provider(self):
        self.assertEqual(self.db.add_extra_model("openai", "my-finetune-1"), ["my-finetune-1"])
        self.assertEqual(self.db.add_extra_model("openai", "my-finetune-1"), ["my-finetune-1"])   # no dupe
        self.assertEqual(self.db.add_extra_model("openai", " other:latest\n"), ["my-finetune-1", "other:latest"])
        self.assertEqual(self.db.remove_extra_model("openai", "my-finetune-1"), ["other:latest"])
        self.assertEqual(self.db.extra_models("anthropic"), [])                                     # per-provider

    def test_a_model_the_provider_already_lists_is_not_saved_twice(self):
        builtin = self.db.all_providers()["openai"]["models"][0]
        self.assertEqual(self.db.add_extra_model("openai", builtin), [])

    def test_invalid_model_ids_and_unknown_providers_are_rejected(self):
        for bad in ["", "two words", "a;rm -rf", "../../x", "x" * 300, "$(id)"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.db.add_extra_model("openai", bad)
        with self.assertRaises(LookupError):
            self.db.add_extra_model("no-such-provider", "m")
        with self.assertRaises(LookupError):
            self.db.remove_extra_model("no-such-provider", "m")

    def test_the_selected_model_flows_into_llm_config(self):
        self.db.add_extra_model("openai", "my-finetune-1")
        self.db.write("openai_model", "my-finetune-1")
        self.db.write("llm_provider", "openai")
        self.assertEqual(self.db.llm_config()["model"], "my-finetune-1")

    def test_cap(self):
        import db.settings_db as m
        old, m.MAX_EXTRA_MODELS = m.MAX_EXTRA_MODELS, 2
        try:
            self.db.add_extra_model("openai", "m1"); self.db.add_extra_model("openai", "m2")
            with self.assertRaises(ValueError):
                self.db.add_extra_model("openai", "m3")
        finally:
            m.MAX_EXTRA_MODELS = old

    def test_custom_provider_models_are_managed_the_same_way(self):
        p = self.db.add_custom_provider("X", "https://x.example.com/v1")
        self.db.add_extra_model(p["id"], "some/model:free")
        self.assertEqual(self.db.all_providers()[p["id"]]["extra_models"], ["some/model:free"])


class AllProvidersShapeTests(ProviderTestBase):
    def test_shape(self):
        p = self.db.add_custom_provider("Groq", "https://api.groq.com/openai/v1", api_key="gsk_abc")
        allp = self.db.all_providers()
        for pid in ("mistral", "nvidia", "openrouter", "gemini", "ollama", "anthropic", "openai"):
            self.assertFalse(allp[pid]["custom"], pid)
            self.assertIn("models", allp[pid]); self.assertIn("extra_models", allp[pid])
            self.assertIsInstance(allp[pid]["configured"], bool)
        self.assertTrue(allp[p["id"]]["custom"])
        self.assertEqual(allp[p["id"]]["base_url"], "https://api.groq.com/openai/v1")
        self.assertTrue(allp[p["id"]]["configured"])
        json.dumps(allp)                                    # must be serialisable for the API
        self.assertNotIn("gsk_abc", json.dumps(allp))       # and must never carry a secret


if __name__ == "__main__":
    unittest.main(verbosity=2)
