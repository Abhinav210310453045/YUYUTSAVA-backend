"""A master's stores take their models from the models service, not from the environment.

``build_master_stores`` builds each chat or tinker master's compaction model and
context budget. Both come from ``TaskModels``: the compaction model is the
``compaction`` task's, and the budget is the window of the model the master
talks on. Retrieval stores (Postgres, embedder) are not what this is about and
are left out.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test.support.targets import anthropic_target
from yuyutsava.core import stores as stores_module
from yuyutsava.llm.factory import model_name_of
from yuyutsava.llm.selection import TaskModels
from yuyutsava.skills.registry import SkillRegistry


class StoresTakeTheirModelsFromTheService(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_model_and_window(self) -> None:
        env = {
            "LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "k",
            "COMPACTION_ANTHROPIC_MODEL": "claude-sonnet-4-6",
            "YUYUTSAVA_STORAGE_BACKEND": "sqlite",
        }
        no_retrieval = mock.AsyncMock(return_value=(None, None, None, None))
        with tempfile.TemporaryDirectory() as home:
            with (
                mock.patch.dict(os.environ, {**env, "YUYUTSAVA_HOME": home}, clear=True),
                mock.patch.object(stores_module, "build_retrieval_stores", no_retrieval),
            ):
                built = await stores_module.build_master_stores(
                    "cli",
                    SkillRegistry(workspace_dir=Path(home) / "skills"),
                    models=TaskModels(),
                    chat=anthropic_target("claude-haiku-4-5-20251001"),
                )
        # The chat model's window (Haiku 4.5: 200k), not the compaction model's (Sonnet 4.6: 1M).
        self.assertEqual(built.context_settings.max_input_tokens, 200_000)
        self.assertEqual(model_name_of(built.compaction_model), "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
