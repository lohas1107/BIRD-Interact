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


def _profile(name="custom", tools=None, prompt="custom prompt"):
    return {
        "name": name,
        "tools": ["get_schema"] if tools is None else tools,
        "prompt_template": prompt,
    }


class ApiCliEvaluationTests(unittest.TestCase):
    def test_init_accepts_top_level_snapshot_and_returns_metadata_only(self):
        async def scenario():
            server.runtime._sessions.clear()
            response = await server.init_session(
                server.SessionInitRequest(
                    task_id="api-profile",
                    mode="a-interact",
                    state={"db_name": "db"},
                    agent_profile=_profile(),
                )
            )
            self.assertEqual(response["agent_profile"], {"name": "custom", "tools": ["get_schema"]})
            state = server.runtime._sessions[("a-interact", "api-profile")].state
            self.assertEqual(state["agent_profile"], response["agent_profile"])
            self.assertNotIn("prompt_template", state["agent_profile"])
            self.assertEqual(
                server.runtime._sessions[("a-interact", "api-profile")].messages[0]["content"],
                "custom prompt",
            )
            await server.runtime.cleanup_session("api-profile", "a-interact")

        asyncio.run(scenario())

    def test_init_rejects_invalid_profile_with_stable_detail(self):
        async def scenario():
            server.runtime._sessions.clear()
            with self.assertRaises(Exception) as raised:
                await server.init_session(
                    server.SessionInitRequest(
                        task_id="api-invalid",
                        agent_profile=_profile(tools=["search_semantic_context"]),
                    )
                )
            exc = raised.exception
            self.assertEqual(exc.status_code, 400)
            self.assertEqual(
                exc.detail,
                {
                    "code": "INVALID_AGENT_PROFILE",
                    "profile": "custom",
                    "field": "tools",
                    "message": "unknown tool 'search_semantic_context'",
                },
            )

        asyncio.run(scenario())

    def test_missing_snapshot_resolves_configured_default(self):
        async def scenario():
            server.runtime._sessions.clear()
            response = await server.init_session(
                server.SessionInitRequest(task_id="default-profile", mode="c-interact")
            )
            self.assertEqual(response["agent_profile"]["name"], "c-interact-default")
            self.assertEqual(response["agent_profile"]["tools"], ["ask_user", "submit_sql"])
            await server.runtime.cleanup_session("default-profile", "c-interact")

        asyncio.run(scenario())

    def test_runner_output_has_top_level_metadata_without_prompt_text(self):
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
                agent_profile=_profile("empty", [], "secret prompt"),
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "result.json"
            asyncio.run(scenario(path))
            output = json.loads(path.read_text())
        self.assertEqual(output["mode"], "a-interact")
        self.assertEqual(output["agent_profile"], {"name": "empty", "tools": []})
        self.assertNotIn("prompt_template", output["agent_profile"])
        self.assertNotIn("agent_profile", output["results"][0])

    def test_runner_invalid_profile_fails_before_loading_task_data(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        command = [
            sys.executable,
            "-m",
            "orchestrator.runner",
            "--mode",
            "a-interact",
            "--agent-profile",
            "missing-profile",
            "--data",
            "/path/that/does/not/exist.json",
        ]
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown agent profile", result.stderr)
        self.assertNotIn("No such file", result.stderr)


if __name__ == "__main__":
    unittest.main()
