import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAIInvocationContract(unittest.TestCase):
    def test_request_evidence_is_hash_only_and_carries_route_identity(self):
        from ai_invocation import AIInvocationRequest, RoutePromptContract

        request = AIInvocationRequest(
            role="observer",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode="codex_cli",
            cwd="/repo",
            prompt="private prompt text",
            system_prompt="private system text",
            route=RoutePromptContract(
                route_context_hash="sha256:route",
                prompt_contract_id="rprompt-1",
                prompt_contract_hash="sha256:prompt",
                route_token_ref="rtok-1",
            ),
        )

        evidence = request.to_evidence()

        self.assertEqual(evidence["schema_version"], "ai_invocation_request.v1")
        self.assertEqual(evidence["provider"], "openai")
        self.assertEqual(evidence["backend_mode"], "codex_cli")
        self.assertEqual(evidence["route_prompt_contract"]["route_context_hash"], "sha256:route")
        self.assertEqual(evidence["route_prompt_contract"]["prompt_contract_id"], "rprompt-1")
        self.assertTrue(evidence["prompt_sha256"].startswith("sha256:"))
        self.assertNotIn("private prompt text", str(evidence))
        self.assertNotIn("private system text", str(evidence))
        self.assertFalse(evidence["raw_prompt_exposed"])

    def test_fixture_invocation_uses_result_schema_without_model_call(self):
        from ai_invocation import AIInvocationRequest, RoutePromptContract, invoke_ai

        request = AIInvocationRequest(
            role="tester",
            provider="fixture",
            backend_mode="fixture",
            prompt="return fixture",
            route=RoutePromptContract(route_context_hash="sha256:route"),
        )

        result = invoke_ai(request)
        evidence = result.to_evidence()

        self.assertEqual(result.status, "completed")
        self.assertEqual(evidence["schema_version"], "ai_invocation_result.v1")
        self.assertFalse(evidence["provider_backed"])
        self.assertFalse(evidence["calls_models"])
        self.assertFalse(evidence["raw_output_stored"])
        self.assertTrue(evidence["no_raw_prompt_output"])
        self.assertEqual(evidence["route_alert_ack"]["status"], "acknowledged")
        self.assertGreaterEqual(len(evidence["ordered_step_outputs"]), 3)

    def test_missing_openai_api_key_fails_closed_with_sanitized_evidence(self):
        from ai_invocation import AIInvocationRequest, BACKEND_OPENAI_API, invoke_ai

        request = AIInvocationRequest(
            role="dev",
            provider="openai",
            model="gpt-4o",
            backend_mode=BACKEND_OPENAI_API,
            prompt="private api prompt",
            auth_mode="api_key_env",
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            result = invoke_ai(request)

        evidence = result.to_evidence()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(evidence["auth_status"], "missing_api_key")
        self.assertTrue(evidence["provider_backed"])
        self.assertFalse(evidence["calls_models"])
        self.assertIn("OPENAI_API_KEY not set", evidence["error"])
        self.assertNotIn("private api prompt", str(evidence))

    def test_backends_run_via_api_returns_invocation_evidence_on_missing_key(self):
        import backends

        task = {
            "id": "task-1",
            "role": "dev",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-1",
        }

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            run = backends.run_via_api(
                task,
                prompt_override="private task prompt",
                model_override="gpt-4o",
                provider_override="openai",
            )

        self.assertEqual(run["returncode"], 1)
        self.assertIn("ai_invocation", run)
        evidence = run["ai_invocation"]
        self.assertEqual(evidence["backend_mode"], "openai_api")
        self.assertEqual(evidence["route_prompt_contract"]["route_context_hash"], "sha256:route")
        self.assertEqual(evidence["route_prompt_contract"]["prompt_contract_id"], "rprompt-1")
        self.assertFalse(evidence["calls_models"])
        self.assertNotIn("private task prompt", str(evidence))


if __name__ == "__main__":
    unittest.main()
