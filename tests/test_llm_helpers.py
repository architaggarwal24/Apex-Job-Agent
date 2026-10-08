"""
tests/test_llm_helpers.py — error translation, unsaved-config connection testing,
and model listing. No network: SDK clients are faked.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import llm
from core.llm import LLMTestError, friendly_llm_error


def exc(name, message="boom", status=None):
    """An exception whose CLASS NAME matches the SDK's (that's what the translator keys on)."""
    e = type(name, (Exception,), {})(message)
    if status is not None:
        e.status_code = status
    return e


CFG = {"type": "openai_compat", "provider": "custom-cc", "api_key": "cc_supersecretkey123",
       "base_url": "https://codecraftapi.com/v1", "model": "claude-opus-4.8"}


class FriendlyErrorTests(unittest.TestCase):
    def msg(self, e, **over):
        return friendly_llm_error(e, {**CFG, **over})

    def test_each_common_failure_says_what_to_fix(self):
        self.assertIn("API key", self.msg(exc("AuthenticationError", status=401)))
        self.assertIn("API key", self.msg(exc("Whatever", status=401)))
        self.assertIn("403", self.msg(exc("PermissionDeniedError", status=403)))
        nf = self.msg(exc("NotFoundError", status=404))
        self.assertIn("claude-opus-4.8", nf); self.assertIn("/v1", nf); self.assertIn("Fetch models", nf)
        self.assertIn("429", self.msg(exc("RateLimitError", status=429)))
        self.assertIn("codecraftapi.com", self.msg(exc("APIConnectionError", "Connection error.")))
        self.assertIn("Timed out", self.msg(exc("APITimeoutError", "Request timed out.")))
        self.assertIn("400", self.msg(exc("BadRequestError", "max_tokens too large", status=400)))
        self.assertIn("max_tokens too large", self.msg(exc("BadRequestError", "max_tokens too large", status=400)))
        self.assertIn("internal error (503)", self.msg(exc("InternalServerError", status=503)))

    def test_unknown_errors_fall_through_to_their_own_text(self):
        self.assertEqual(self.msg(exc("Weird", "something odd")), "something odd")

    def test_the_api_key_is_never_echoed_back_to_the_user(self):
        leaky = exc("BadRequestError", "bad request for key cc_supersecretkey123 here", status=400)
        self.assertNotIn("cc_supersecretkey123", self.msg(leaky))
        self.assertIn("***", self.msg(leaky))

    def test_very_long_provider_messages_are_truncated(self):
        self.assertLessEqual(len(self.msg(exc("BadRequestError", "x" * 5000, status=400))), 400)

    def test_missing_base_url_and_model_dont_crash_the_translator(self):
        self.assertIsInstance(friendly_llm_error(exc("NotFoundError", status=404), {"type": "gemini"}), str)


class TestConnectionTests(unittest.TestCase):
    def fake_client(self, text="OK", raises=None):
        create = mock.Mock(side_effect=raises) if raises else mock.Mock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))]))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create

    def setUp(self):
        llm.reset_clients()

    def test_success_uses_the_given_config_and_never_touches_the_cached_client(self):
        client, create = self.fake_client("OK")
        with mock.patch.object(llm, "_openai_client_for", return_value=client) as make:
            out = llm.test_connection(CFG)
        self.assertEqual(out["response"], "OK"); self.assertEqual(out["note"], "")
        self.assertIsInstance(out["latency_ms"], int)
        make.assert_called_once_with(CFG)
        self.assertEqual(create.call_args.kwargs["model"], "claude-opus-4.8")
        self.assertGreaterEqual(create.call_args.kwargs["max_tokens"], 256, "reasoning models need headroom")
        self.assertIsNone(llm._openai_client, "a test must not populate the live client cache")

    def test_empty_reply_is_reported_as_connected_with_an_explanation_not_a_failure(self):
        client, _ = self.fake_client("")
        with mock.patch.object(llm, "_openai_client_for", return_value=client):
            out = llm.test_connection(CFG)
        self.assertEqual(out["response"], ""); self.assertIn("reasoning", out["note"])

    def test_sdk_errors_become_friendly_messages(self):
        client, _ = self.fake_client(raises=exc("AuthenticationError", "nope", status=401))
        with mock.patch.object(llm, "_openai_client_for", return_value=client):
            with self.assertRaises(LLMTestError) as cm:
                llm.test_connection(CFG)
        self.assertIn("API key", str(cm.exception))

    def test_missing_model_or_key_is_caught_before_any_network_call(self):
        with mock.patch.object(llm, "_openai_client_for") as make:
            with self.assertRaises(LLMTestError):
                llm.test_connection({**CFG, "model": ""})
            with self.assertRaises(LLMTestError):
                llm.test_connection({**CFG, "api_key": ""})
            with self.assertRaises(LLMTestError):
                llm.test_connection({**CFG, "type": "anthropic", "api_key": ""})
            make.assert_not_called()

    def test_ollama_needs_no_key(self):
        client, _ = self.fake_client("OK")
        cfg = {**CFG, "provider": "ollama", "api_key": "ollama", "base_url": "http://localhost:11434/v1"}
        with mock.patch.object(llm, "_openai_client_for", return_value=client):
            self.assertEqual(llm.test_connection(cfg)["response"], "OK")


class ListModelsTests(unittest.TestCase):
    def test_returns_sorted_unique_ids(self):
        models = SimpleNamespace(list=lambda: [SimpleNamespace(id=i) for i in ["b", "a", "b", "", "c/d"]])
        with mock.patch.object(llm, "_openai_client_for", return_value=SimpleNamespace(models=models)):
            self.assertEqual(llm.list_models(CFG), ["a", "b", "c/d"])

    def test_failures_are_friendly(self):
        def boom(): raise exc("APIConnectionError", "Connection error.")
        with mock.patch.object(llm, "_openai_client_for", return_value=SimpleNamespace(models=SimpleNamespace(list=boom))):
            with self.assertRaises(LLMTestError) as cm:
                llm.list_models(CFG)
        self.assertIn("Couldn't connect", str(cm.exception))

    def test_result_is_capped(self):
        models = SimpleNamespace(list=lambda: [SimpleNamespace(id=f"m{i:04d}") for i in range(2000)])
        with mock.patch.object(llm, "_openai_client_for", return_value=SimpleNamespace(models=models)):
            self.assertEqual(len(llm.list_models(CFG)), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
