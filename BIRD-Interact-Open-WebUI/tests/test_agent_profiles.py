import json
import tempfile
import unittest
from pathlib import Path

from shared.agent_profiles import (
    VALID_TOOLS,
    InvalidAgentProfile,
    load_agent_profiles,
    normalize_agent_profile,
    resolve_agent_profile,
)


class AgentProfileTests(unittest.TestCase):
    def _config(self, raw, prompts=None):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        for name, contents in (prompts or {"default.md": ""}).items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        path = directory / "agent_profiles.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_default_schema_and_order(self):
        a_profile = resolve_agent_profile("a-interact")
        c_profile = resolve_agent_profile("c-interact")
        self.assertEqual(a_profile.name, "a-interact-default")
        self.assertEqual(
            a_profile.tools,
            (
                "ask_user",
                "get_schema",
                "get_all_column_meanings",
                "get_column_meaning",
                "get_all_external_knowledge_names",
                "get_knowledge_definition",
                "get_all_knowledge_definitions",
                "execute_sql",
                "submit_sql",
            ),
        )
        self.assertEqual(c_profile.tools, ("ask_user", "submit_sql"))
        self.assertEqual(set(a_profile.tools), set(VALID_TOOLS))
        self.assertIn("{{db_name}}", c_profile.prompt_template)

    def test_custom_profiles_allow_empty_prompt_and_cross_mode_tools(self):
        path = self._config(
            {
                "defaults": {"a-interact": "custom", "c-interact": "empty"},
                "profiles": {
                    "custom": {"prompt_file": "nested/custom.md", "tools": ["get_schema", "ask_user"]},
                    "empty": {"prompt_file": "default.md", "tools": []},
                },
            },
            {"default.md": "", "nested/custom.md": "Custom {{available_tools}}"},
        )
        config = load_agent_profiles(path)
        self.assertEqual(config.defaults["a-interact"], "custom")
        self.assertEqual(config.profiles["custom"].tools, ("get_schema", "ask_user"))
        self.assertEqual(config.profiles["empty"].prompt_template, "")
        self.assertEqual(resolve_agent_profile("c-interact", "custom", path).tools, ("get_schema", "ask_user"))

    def test_prompt_file_is_relative_and_cannot_escape_config_directory(self):
        outside = Path(tempfile.mkstemp()[1])
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        path = self._config(
            {
                "defaults": {"a-interact": "p"},
                "profiles": {"p": {"prompt_file": "../outside.md", "tools": []}},
            }
        )
        with self.assertRaisesRegex(InvalidAgentProfile, "within"):
            load_agent_profiles(path)

    def test_validation_rejects_missing_files_tools_placeholders_and_defaults(self):
        cases = [
            (
                {"defaults": {"a-interact": "p"}, "profiles": {"p": {"tools": []}}},
                {},
                "prompt_file",
            ),
            (
                {"defaults": {"a-interact": "p"}, "profiles": {"p": {"prompt_file": "missing.md", "tools": []}}},
                {},
                "cannot read prompt_file",
            ),
            (
                {"defaults": {"a-interact": "p"}, "profiles": {"p": {"prompt_file": "default.md", "tools": ["unknown"]}}},
                {"default.md": ""},
                "unknown tool",
            ),
            (
                {"defaults": {"a-interact": "p"}, "profiles": {"p": {"prompt_file": "default.md", "tools": ["ask_user", "ask_user"]}}},
                {"default.md": ""},
                "duplicate tool",
            ),
            (
                {"defaults": {"a-interact": "p"}, "profiles": {"p": {"prompt_file": "default.md", "tools": []}}},
                {"default.md": "{{unknown}}"},
                "unknown prompt placeholder",
            ),
            (
                {"defaults": {"a-interact": "missing"}, "profiles": {}},
                {},
                "unknown agent profile",
            ),
        ]
        for raw, prompts, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    load_agent_profiles(self._config(raw, prompts))

    def test_snapshot_requires_prompt_template_and_ignores_mode_restrictions(self):
        snapshot = {
            "name": "cross-mode",
            "tools": ["get_schema"],
            "prompt_template": "DB={{db_name}}",
        }
        profile = normalize_agent_profile("c-interact", snapshot)
        self.assertEqual(profile.as_snapshot(), snapshot)
        with self.assertRaisesRegex(InvalidAgentProfile, "prompt_template"):
            normalize_agent_profile("a-interact", {"name": "incomplete", "tools": []})


if __name__ == "__main__":
    unittest.main()
