"""Explanation fixtures only: this suite never contacts an API or reads a key."""
import ast
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from banknifty.explanation_service import (ExplanationConfig, ExplanationError,
    ExplanationService, HTTPSResponsesTransport, local_explanation)
from tests.test_options_strategy import candidate


def config(**changes):
    return replace(ExplanationConfig(enabled=True, model="fixture-model", max_requests=3,
        max_reserved_tokens=64000, max_estimated_usd=Decimal("1"),
        input_usd_per_million=Decimal("1"), output_usd_per_million=Decimal("2")), **changes)


def envelope(fingerprint, ids=None, **changes):
    document = {"input_fingerprint": fingerprint, "fact_ids": ids or ["structure", "pricing"]}
    document.update(changes)
    return json.dumps({"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": json.dumps(document)}]}]}).encode()


class FixtureTransport:
    network_capable = False

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0
        self.body = None
        self.options = None

    def send(self, body, api_key, **options):
        self.calls += 1
        self.body = body
        self.options = options
        # Deliberately retain no fixture key.
        request = json.loads(body)
        fingerprint = json.loads(request["input"][0]["content"])["input_fingerprint"]
        return self.payload if self.payload is not None else envelope(fingerprint)


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.signal = candidate()
        self.key = "FIXTURE-ONLY-EPHEMERAL-KEY"

    def test_default_is_local_and_does_not_touch_transport(self):
        transport = FixtureTransport()
        result = ExplanationService(transport=transport).explain(self.signal, api_key=self.key, allow_network=True)
        self.assertEqual((result.mode, result.status, transport.calls), ("local", "disabled", 0))
        self.assertIn("no API request", result.text)
        self.assertIn(self.signal.input_fingerprint, result.text)
        self.assertIn("synthetic", result.text)

    def test_deterministic_frozen_result_does_not_modify_signal(self):
        before = self.signal.to_dict()
        first = local_explanation(self.signal)
        self.assertEqual(first, local_explanation(self.signal))
        self.assertEqual(before, self.signal.to_dict())
        with self.assertRaises(FrozenInstanceError):
            first.mode = "api"

    def test_no_trade_and_missing_sizing_remain_explicit(self):
        result = local_explanation(replace(self.signal, status="no_trade", legs=(), quantity=None))
        self.assertIn("cannot override", result.text)
        self.assertIn("Risk sizing is unavailable", result.text)
        self.assertIn("not a guaranteed loss cap", result.text)

    def test_derived_pipeline_direction_and_evidence_remain_grounded(self):
        signal = replace(self.signal, underlying_quality="timestamped_closed_bar_structural_checks_passed",
                         label="live_data_verified_research_unvalidated")
        result = local_explanation(signal)
        self.assertIn("this explanation did not generate it", result.text)
        self.assertNotIn("caller-supplied research hypothesis", result.text)
        self.assertIn("recorded structural checks", result.text)

    def test_requires_explicit_model_budget_and_no_implicit_network(self):
        for cfg in (config(model=None), config(input_usd_per_million=None), config(max_estimated_usd=None)):
            transport = FixtureTransport()
            result = ExplanationService(cfg, transport=transport).explain(self.signal, api_key=self.key)
            self.assertEqual(result.status, "configuration_required")
            self.assertEqual(transport.calls, 0)
        with patch("banknifty.explanation_service.HTTPSResponsesTransport.send", side_effect=AssertionError("network forbidden")):
            result = ExplanationService(config()).explain(self.signal, api_key=self.key)
        self.assertEqual(result.status, "network_disabled")

    def test_missing_or_header_injection_key_never_sends(self):
        for key in (None, "", "key\r\nHeader: injected", "key value", "x" * 513, "unicode-\u20b9"):
            transport = FixtureTransport()
            result = ExplanationService(config(), transport=transport).explain(self.signal, api_key=key)
            self.assertEqual(result.status, "credential_required")
            self.assertEqual(transport.calls, 0)

    def test_request_strict_schema_store_false_and_privacy_minimization(self):
        signal = replace(self.signal, reasons=("api_key=LOCAL-SECRET hello@example.com https://private/path",),
                         sizing_reasons=("personal-account-content",))
        transport = FixtureTransport()
        result = ExplanationService(config(), transport=transport).explain(signal, api_key=self.key)
        request = json.loads(transport.body)
        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "fixture-model")
        self.assertEqual(request["max_output_tokens"], 512)
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertFalse(request["text"]["format"]["schema"]["additionalProperties"])
        self.assertNotIn("tools", request)
        self.assertNotIn("previous_response_id", request)
        for excluded in (self.key, "LOCAL-SECRET", "hello@example.com", "personal-account-content", "book fixture", "catalogue fixture", "capital", "entry_net"):
            self.assertNotIn(excluded.encode(), transport.body)
        self.assertNotIn("LOCAL-SECRET", result.text)
        self.assertIn("FIXTURE ONLY", result.notice)
        self.assertFalse(result.remote_attempted)
        self.assertEqual(transport.options["timeout_seconds"], 10)

    def test_fixture_result_cannot_remove_mandatory_risk_facts(self):
        transport = FixtureTransport(envelope(self.signal.input_fingerprint, ["structure"]))
        result = ExplanationService(config(), transport=transport).explain(self.signal, api_key=self.key)
        self.assertEqual(result.mode, "fixture")
        ids = {fact.fact_id for fact in result.facts}
        self.assertTrue({"status", "evidence", "limitations", "execution", "sizing", "stops", "underlying"} <= ids)

    def test_free_prose_prices_direction_execution_and_unknown_facts_are_rejected(self):
        documents = [envelope(self.signal.input_fingerprint, text="Buy now at 500 with 99% confidence"),
            envelope(self.signal.input_fingerprint, ["Buy now"]),
            envelope(self.signal.input_fingerprint, [500]),
            envelope(self.signal.input_fingerprint, ["direction_bearish"]),
            envelope("0" * 64), envelope(self.signal.input_fingerprint, ["status", "status"])]
        for payload in documents:
            with self.subTest(payload=payload):
                result = ExplanationService(config(), transport=FixtureTransport(payload)).explain(self.signal, api_key=self.key)
                self.assertEqual((result.mode, result.status), ("local", "rejected_or_failed"))
                self.assertNotIn("99%", result.text)

    def test_incomplete_refusal_tools_and_malformed_json_fail_closed(self):
        responses = [b"not json", b'{"status":"completed","status":"completed","output":[]}',
            json.dumps({"status": "incomplete", "output": []}).encode(),
            json.dumps({"status": "completed", "output": [{"type": "function_call", "name": "place_order"}]}).encode(),
            json.dumps({"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "raw sensitive refusal"}]}]}).encode(),
            b'{"status":"completed","error":NaN,"output":[]}']
        for payload in responses:
            result = ExplanationService(config(), transport=FixtureTransport(payload)).explain(self.signal, api_key=self.key)
            self.assertEqual(result.status, "rejected_or_failed")
            self.assertNotIn("raw sensitive", result.text)

    def test_failed_transport_errors_are_sanitized_and_not_retried(self):
        class Broken(FixtureTransport):
            def send(self, body, api_key, **options):
                self.calls += 1
                raise TimeoutError("secret token " + api_key + " private provider body")
        transport = Broken()
        service = ExplanationService(config(), transport=transport)
        result = service.explain(self.signal, api_key=self.key)
        self.assertEqual(result.status, "rejected_or_failed")
        self.assertNotIn(self.key, str(result.to_dict()))
        self.assertNotIn("private provider body", result.text)
        cached = service.explain(self.signal, api_key=self.key)
        self.assertTrue(cached.cached)
        self.assertEqual(transport.calls, 1)

    def test_repeated_click_caches_without_another_request(self):
        transport = FixtureTransport()
        service = ExplanationService(config(max_requests=1), transport=transport)
        first = service.explain(self.signal, api_key=self.key)
        second = service.explain(self.signal, api_key=self.key)
        self.assertEqual(first.facts, second.facts)
        self.assertTrue(second.cached)
        self.assertEqual(transport.calls, 1)
        other = replace(self.signal, input_fingerprint="b" * 64)
        self.assertEqual(service.explain(other, api_key=self.key).status, "budget_exhausted")

    def test_request_input_output_and_estimated_cost_budgets_fail_closed(self):
        for cfg, status in ((config(max_input_bytes=100), "input_limit"),
                            (config(max_reserved_tokens=10), "budget_exhausted"),
                            (config(max_estimated_usd=Decimal("0.000001")), "budget_exhausted")):
            transport = FixtureTransport()
            result = ExplanationService(cfg, transport=transport).explain(self.signal, api_key=self.key)
            self.assertEqual(result.status, status)
            self.assertEqual(transport.calls, 0)
        oversized = ExplanationService(config(max_response_bytes=100), transport=FixtureTransport(b"x" * 101))
        self.assertEqual(oversized.explain(self.signal, api_key=self.key).status, "rejected_or_failed")
        self.assertEqual(oversized.budget_state["requests"], 1)

    def test_cancel_before_send_and_after_response_never_emits_model_selection(self):
        transport = FixtureTransport()
        result = ExplanationService(config(), transport=transport).explain(self.signal, api_key=self.key, cancelled=lambda: True)
        self.assertEqual((result.status, transport.calls), ("cancelled", 0))
        checks = iter((False, False, True))
        service = ExplanationService(config(), transport=transport)
        result = service.explain(self.signal, api_key=self.key, cancelled=lambda: next(checks))
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.mode, "local")
        self.assertFalse(result.remote_attempted)
        self.assertEqual(service.budget_state["requests"], 1)

    def test_concurrent_same_record_is_not_sent_twice(self):
        entered, release = threading.Event(), threading.Event()
        class SlowFixture(FixtureTransport):
            def send(self, body, api_key, **options):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("fixture release timed out")
                return super().send(body, api_key, **options)
        transport = SlowFixture()
        service = ExplanationService(config(), transport=transport)
        thread = threading.Thread(target=lambda: service.explain(self.signal, api_key=self.key))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            result = service.explain(self.signal, api_key=self.key)
            self.assertEqual(result.status, "request_in_progress")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(transport.calls, 1)

    def test_credentials_and_environment_stores_are_not_read_or_retained(self):
        service = ExplanationService(config(), transport=FixtureTransport())
        with patch("builtins.open", side_effect=AssertionError("No files")), patch.dict("os.environ", {"OPENAI_API_KEY": self.key}):
            self.assertEqual(service.explain(self.signal).status, "credential_required")
            service.explain(self.signal, api_key=self.key)
        self.assertNotIn(self.key, repr(service.__dict__))
        source = (Path(__file__).resolve().parents[1] / "banknifty/explanation_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name.endswith(("groww", "manual_ledger", "signal_journal")) for name in imports))
        self.assertNotIn("getenv", source)
        self.assertNotIn("os.environ", source)

    def test_bad_configs_and_invalid_research_claims_are_rejected(self):
        for kwargs in ({"enabled": "yes"}, {"model": "bad\nheader"}, {"timeout_seconds": float("nan")},
                       {"max_requests": 0}, {"max_output_tokens": True}, {"max_estimated_usd": Decimal("NaN")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ExplanationConfig(**kwargs)
        for signal in (replace(self.signal, trading_allowed=True), replace(self.signal, calibrated=True),
                       replace(self.signal, confidence=0.9), replace(self.signal, input_fingerprint="bad")):
            with self.assertRaises(ValueError):
                local_explanation(signal)


class HTTPBoundaryTests(unittest.TestCase):
    def test_injected_shipped_https_cannot_bypass_network_permission(self):
        signal = candidate()
        class DisguisedHTTPS(HTTPSResponsesTransport):
            network_capable = False
        for transport in (HTTPSResponsesTransport(), DisguisedHTTPS()):
            with self.subTest(transport=type(transport).__name__), patch(
                    "banknifty.explanation_service.http.client.HTTPSConnection") as connection:
                service = ExplanationService(config(), transport=transport)
                result = service.explain(signal, api_key="FIXTURE-ONLY-KEY", allow_network=False)
                connection.assert_not_called()
                self.assertEqual((result.mode, result.status, result.remote_attempted),
                                 ("local", "network_disabled", False))
                self.assertEqual(service.budget_state["requests"], 0)

    def test_unknown_or_invalid_transport_capability_requires_permission(self):
        class Unknown:
            def send(self, *args, **kwargs):
                raise AssertionError("Unapproved transport must never be invoked")
        for capability in (None, 0, "false", True):
            transport = Unknown()
            if capability is not None:
                transport.network_capable = capability
            result = ExplanationService(config(), transport=transport).explain(
                candidate(), api_key="FIXTURE-ONLY-KEY")
            self.assertEqual(result.status, "network_disabled")
            self.assertFalse(result.remote_attempted)

    def test_injected_https_attempt_is_reported_truthfully_on_success_failure_and_cancel(self):
        signal = candidate()
        class Response:
            status = 200
            def read(self, size):
                return envelope(signal.input_fingerprint)
        class Connection:
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        for outcome in ("success", "failure", "cancel_before_send", "cancel_after_response"):
            connection = Connection()
            if outcome == "failure":
                connection.request = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture error"))
            checks = iter((False, True) if outcome == "cancel_before_send" else (False, False, True))
            cancelled = (lambda: next(checks)) if outcome.startswith("cancel_") else None
            with self.subTest(outcome=outcome), patch(
                    "banknifty.explanation_service.http.client.HTTPSConnection", return_value=connection) as factory:
                result = ExplanationService(config(), transport=HTTPSResponsesTransport()).explain(
                    signal, api_key="FIXTURE-ONLY-KEY", allow_network=True, cancelled=cancelled)
                self.assertEqual(result.remote_attempted, outcome != "cancel_before_send")
                self.assertEqual(factory.call_count, int(outcome != "cancel_before_send"))
                self.assertNotEqual(result.mode, "fixture")
                self.assertNotIn("no API call occurred", result.notice)
                if outcome == "success":
                    self.assertEqual((result.mode, result.status), ("api", "completed"))
                elif outcome == "failure":
                    self.assertEqual(result.status, "rejected_or_failed")
                else:
                    self.assertEqual(result.status, "cancelled")

    def test_fixed_https_post_timeout_no_redirect_or_provider_body(self):
        class Response:
            status = 302
            def read(self, size):
                raise AssertionError("Redirect or error body must not be read")
        class Connection:
            def __init__(self):
                self.closed = False
            def request(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs
            def getresponse(self):
                return Response()
            def close(self):
                self.closed = True
        connection = Connection()
        with patch("banknifty.explanation_service.http.client.HTTPSConnection", return_value=connection) as factory:
            with self.assertRaises(ExplanationError) as caught:
                HTTPSResponsesTransport().send(b"{}", "FIXTURE", timeout_seconds=2, max_response_bytes=100)
        self.assertEqual(factory.call_args.args, ("api.openai.com", 443))
        self.assertEqual(factory.call_args.kwargs["timeout"], 2)
        self.assertEqual(connection.args[:2], ("POST", "/v1/responses"))
        self.assertTrue(connection.closed)
        self.assertNotIn("FIXTURE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
