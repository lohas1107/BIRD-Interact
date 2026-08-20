import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_agent import server
from orchestrator.runner import run_parallel_evaluation


ROOT = Path(__file__).resolve().parents[1]


class ApiCliEvaluationTests(unittest.TestCase):
    def test_init_rejects_unsupported_tool_with_stable_detail_and_returns_snapshot(self):
        async def scenario():
            server.runtime._sessions.clear()
            response = await server.init_session(
                server.SessionInitRequest(
                    task_id="api-profile",
                    mode="a-interact",
                    state={"tool_profile": {"name": "custom", "tools": ["get_schema"]}},
                )
            )
            self.assertEqual(response["tool_profile"], {"name": "custom", "tools": ["get_schema"]})
            with self.assertRaises(Exception) as raised:
                await server.init_session(
                    server.SessionInitRequest(
                        task_id="api-invalid",
                        state={
                            "tool_profile": {
                                "name": "custom",
                                "tools": ["search_semantic_context"],
                            }
                        },
                    )
                )
            exc = raised.exception
            self.assertEqual(exc.status_code, 400)
            self.assertEqual(
                exc.detail,
                {
                    "code": "INVALID_TOOL_PROFILE",
                    "profile": "custom",
                    "tool": "search_semantic_context",
                    "message": "tool is not supported by the Open WebUI P0 registry",
                },
            )
            await server.runtime.cleanup_session("api-profile", "a-interact")

        asyncio.run(scenario())

    def test_runner_output_has_top_level_profile_metadata(self):
        async def fake_task(task):
            return {
                "task_id": task["instance_id"],
                "phase1_passed": False,
                "phase2_passed": False,
                "total_reward": 0.0,
            }

        async def scenario(path):
            await run_parallel_evaluation(
                tasks=[{"instance_id": "one"}],
                run_single_task=fake_task,
                output_path=str(path),
                concurrency=1,
                mode="a-interact",
                tool_profile={"name": "empty", "tools": []},
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "result.json"
            asyncio.run(scenario(path))
            output = json.loads(path.read_text())
        self.assertEqual(output["mode"], "a-interact")
        self.assertEqual(output["tool_profile"], {"name": "empty", "tools": []})
        self.assertNotIn("tool_profile", output["results"][0])

    def test_runner_invalid_profile_fails_before_loading_task_data(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        command = [
            sys.executable,
            "-m",
            "orchestrator.runner",
            "--mode",
            "a-interact",
            "--tool-profile",
            "missing-profile",
            "--data",
            "/path/that/does/not/exist.json",
        ]
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown tool profile", result.stderr)
        self.assertNotIn("No such file", result.stderr)


if __name__ == "__main__":
    unittest.main()
