"""A failed model call is explained in terms the reader can act on (design rule 15).

The one that matters most: Vertex answers a model the project has no quota for
with the same 429 a busy minute gets. Called "provider busy", the user resends
forever; it needs a quota increase.
"""

from __future__ import annotations

import unittest

from google.api_core import exceptions as g
from langchain_core.exceptions import ContextOverflowError

from yuyutsava.llm.failures import describe_model_failure, quota_refusal

#: Claude on Vertex at `global` on a new project, as measured on 2026-10-03.
ZERO_QUOTA = (
    "Error code: 429 - {'error': {'code': 429, 'message': 'Quota exceeded for "
    "aiplatform.googleapis.com/global_online_prediction_requests_per_base_model with base "
    "model: anthropic-claude-haiku-4-5. Please submit a quota increase request. "
    "https://cloud.google.com/vertex-ai/docs/generative-ai/quotas-genai.', "
    "'status': 'RESOURCE_EXHAUSTED'}}"
)


class AQuotaRefusal(unittest.TestCase):
    def test_names_the_model_and_where_to_raise_it(self) -> None:
        refusal = quota_refusal(RuntimeError(ZERO_QUOTA))
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.base_model, "anthropic-claude-haiku-4-5")
        said = describe_model_failure(RuntimeError(ZERO_QUOTA))
        self.assertIn("no quota for anthropic-claude-haiku-4-5", said)
        self.assertIn("console.cloud.google.com/iam-admin/quotas", said)
        self.assertNotIn("busy", said)

    def test_is_not_called_busy_even_as_a_retried_kind(self) -> None:
        said = describe_model_failure(g.ResourceExhausted(ZERO_QUOTA))
        self.assertTrue(said.startswith("no quota"), said)

    def test_carries_the_project_when_the_error_names_it(self) -> None:
        refusal = quota_refusal(
            RuntimeError(f"{ZERO_QUOTA} on projects/project-4a4a8e80-a32c-4e1c-879")
        )
        self.assertIn("?project=project-4a4a8e80-a32c-4e1c-879", refusal.link)


class TheOthers(unittest.TestCase):
    def test_a_busy_provider(self) -> None:
        said = describe_model_failure(g.ServiceUnavailable("try later"))
        self.assertTrue(said.startswith("provider busy (503)"), said)

    def test_a_busy_genai_backend(self) -> None:
        from google.genai import errors

        said = describe_model_failure(errors.ServerError(503, {"error": {"message": "busy"}}))
        self.assertTrue(said.startswith("provider busy (503)"), said)

    def test_too_long_even_summarized(self) -> None:
        self.assertIn("window", describe_model_failure(ContextOverflowError("too long")))

    def test_anything_else_is_left_to_the_caller(self) -> None:
        self.assertIsNone(describe_model_failure(ValueError("tool schema is invalid")))


if __name__ == "__main__":
    unittest.main()
