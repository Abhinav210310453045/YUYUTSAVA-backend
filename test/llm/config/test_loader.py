"""A ``models.toml`` problem is reported where it is, with what is wrong and how to fix it.

Each case pins all four parts of an issue — the setting's path, its line, the
problem and the fix — because the fix is the part a person acts on. The loader
also reports every problem in a file together, not one per attempt.
"""

from __future__ import annotations

import unittest

from yuyutsava.llm.config.loader import ConfigIssue, ModelsConfigError, line_of, load_text

_HEAD = 'version = 1\n\n[defaults]\nmodel = "fast"\n\n'
_GROQ = (
    '[providers.groq]\ntype = "groq"\nauth = { method = "api_key", key = "env:GROQ_API_KEY" }\n\n'
)
_FAST = '[models.fast]\nprovider = "groq"\nid = "llama-3.3-70b-versatile"\n'


def issues_of(text: str) -> list[ConfigIssue]:
    try:
        load_text(text)
    except ModelsConfigError as exc:
        return list(exc.issues)
    raise AssertionError("the file loaded, but a problem was expected")


def only_issue(text: str) -> ConfigIssue:
    issues = issues_of(text)
    if len(issues) != 1:
        raise AssertionError(f"expected one problem, got {len(issues)}: {issues}")
    return issues[0]


class AValidFileLoads(unittest.TestCase):
    def test_the_smallest_useful_file(self) -> None:
        config = load_text(_HEAD + _GROQ + _FAST)
        self.assertEqual(config.models["fast"].id, "llama-3.3-70b-versatile")
        self.assertEqual(config.tasks, {})

    def test_a_task_may_name_a_provider_model_without_an_alias(self) -> None:
        config = load_text(
            _HEAD + '[tasks]\ntriage = "groq/llama-3.1-8b-instant"\n\n' + _GROQ + _FAST
        )
        self.assertEqual(config.tasks["triage"], "groq/llama-3.1-8b-instant")

    def test_a_named_subagent_is_a_task(self) -> None:
        config = load_text(_HEAD + '[tasks]\n"subagent.researcher" = "fast"\n\n' + _GROQ + _FAST)
        self.assertIn("subagent.researcher", config.tasks)

    def test_an_openrouter_id_keeps_its_slashes(self) -> None:
        text = (
            'version = 1\n[defaults]\nmodel = "or/anthropic/claude-sonnet-4.5"\n'
            '[providers.or]\ntype = "openrouter"\nauth = { method = "api_key", key = "secret:or" }\n'
        )
        self.assertEqual(load_text(text).defaults.model, "or/anthropic/claude-sonnet-4.5")


class LoginProblems(unittest.TestCase):
    def test_a_login_the_provider_type_does_not_accept(self) -> None:
        text = (
            _HEAD
            + (
                '[providers.groq]\ntype = "bedrock"\nregion = "us-east-1"\n'
                'auth = { method = "api_key", key = "secret:bedrock" }\n\n'
            )
            + _FAST
        )
        issue = only_issue(text)
        self.assertEqual((issue.path, issue.line), ("providers.groq.auth.method", 9))
        self.assertIn("AWS Bedrock logs in with", issue.problem)
        self.assertIn("not 'api_key'", issue.problem)
        self.assertIn('auth = { method = "bedrock_api_key", key = "secret:bedrock" }', issue.fix)

    def test_the_wrong_method_on_a_one_login_type_is_one_problem_not_two(self) -> None:
        text = _HEAD + '[providers.groq]\ntype = "groq"\nauth = { method = "adc" }\n\n' + _FAST
        issue = only_issue(text)
        self.assertEqual(issue.path, "providers.groq.auth.method")
        self.assertIn("Groq logs in with api_key", issue.problem)
        self.assertIn('key = "secret:groq"', issue.fix)

    def test_a_pasted_key_is_refused_without_repeating_it(self) -> None:
        key = "gsk_abcdefghijklmnopqrstuvwxyz0123456789"
        text = (
            _HEAD
            + f'[providers.groq]\ntype = "groq"\nauth = {{ method = "api_key", key = "{key}" }}\n\n'
            + _FAST
        )
        issue = only_issue(text)
        self.assertEqual(issue.path, "providers.groq.auth.key")
        self.assertIn("pasted in directly", issue.problem)
        self.assertIn("secret:<name>", issue.fix)
        self.assertNotIn(key, issue.problem + issue.fix)

    def test_auth_written_as_text(self) -> None:
        text = _HEAD + '[providers.groq]\ntype = "groq"\nauth = "api_key"\n\n' + _FAST
        issue = only_issue(text)
        self.assertEqual(issue.problem, "auth must be a table with a method")
        self.assertIn('auth = { method = "api_key"', issue.fix)


class ProviderProblems(unittest.TestCase):
    def test_an_unknown_type_suggests_the_close_one(self) -> None:
        text = _HEAD + '[providers.groq]\ntype = "grok"\nauth = { method = "none" }\n\n' + _FAST
        issue = only_issue(text)
        self.assertEqual((issue.path, issue.line), ("providers.groq.type", 7))
        self.assertEqual(issue.problem, "'grok' is not a type of provider")
        self.assertTrue(issue.fix.startswith("did you mean type = 'groq'? Otherwise"))

    def test_a_missing_required_setting(self) -> None:
        text = (
            _HEAD
            + '[providers.groq]\ntype = "vertex"\nlocation = "global"\nauth = { method = "adc" }\n\n'
            + _FAST
        )
        issue = only_issue(text)
        self.assertEqual(issue.path, "providers.groq.project")
        self.assertEqual(issue.problem, "a Google Vertex AI provider needs project")
        self.assertEqual(issue.fix, 'project = "my-gcp-project"')

    def test_a_misspelt_setting_is_not_silently_dropped(self) -> None:
        text = (
            _HEAD
            + '[providers.groq]\ntype = "groq"\nauth = { method = "none" }\nbase_ulr = "x"\n\n'
            + _FAST
        )
        issues = issues_of(text)
        misspelt = next(i for i in issues if i.path.endswith("base_ulr"))
        self.assertEqual(misspelt.line, 9)
        self.assertTrue(misspelt.fix.startswith("did you mean 'base_url'?"))

    def test_a_name_with_a_space(self) -> None:
        text = (
            _HEAD
            + '[providers."my groq"]\ntype = "groq"\nauth = { method = "api_key", key = "env:K" }\n\n'
            + _FAST
        )
        issue = issues_of(text)[0]
        self.assertEqual(issue.problem, "'my groq' cannot be used as a name")

    def test_no_providers_at_all(self) -> None:
        issue = only_issue('version = 1\n[defaults]\nmodel = "x"\n[providers]\n')
        self.assertEqual((issue.path, issue.problem), ("providers", "no providers are set up"))


class ReferenceProblems(unittest.TestCase):
    def test_a_default_that_names_no_model(self) -> None:
        text = _HEAD.replace('"fast"', '"fats"') + _GROQ + _FAST
        issue = only_issue(text)
        self.assertEqual((issue.path, issue.line), ("defaults.model", 4))
        self.assertEqual(issue.problem, "no model is called 'fats'")
        self.assertTrue(
            issue.fix.startswith("did you mean 'fast'? Otherwise add it under [models]")
        )

    def test_a_task_that_is_not_one(self) -> None:
        issue = only_issue(_HEAD + '[tasks]\ncompation = "fast"\n\n' + _GROQ + _FAST)
        self.assertEqual((issue.path, issue.line), ("tasks.compation", 7))
        self.assertIn("did you mean 'compaction'?", issue.fix)

    def test_a_model_on_a_provider_that_does_not_exist(self) -> None:
        issue = only_issue(_HEAD + _GROQ + _FAST.replace('provider = "groq"', 'provider = "grq"'))
        self.assertEqual(issue.path, "models.fast.provider")
        self.assertIn("did you mean provider = 'groq'?", issue.fix)

    def test_a_setting_that_belongs_to_another_provider_type(self) -> None:
        issue = only_issue(_HEAD + _GROQ + _FAST + 'location = "global"\n')
        self.assertEqual(issue.path, "models.fast.location")
        self.assertIn("only to Google Vertex AI models", issue.problem)
        self.assertEqual(issue.fix, "remove location from [models.fast]")

    def test_a_provider_model_reference_to_an_unknown_provider(self) -> None:
        issue = only_issue(_HEAD + '[tasks]\ntriage = "groc/llama"\n\n' + _GROQ + _FAST)
        self.assertEqual(issue.problem, "no provider is called 'groc'")


class EveryProblemAtOnce(unittest.TestCase):
    def test_structure_problems_are_reported_together(self) -> None:
        text = (
            'version = 2\n[defaults]\nmodel = "x"\n'
            '[providers.a]\ntype = "vertex"\nauth = { method = "adc" }\n'
            '[providers.b]\ntype = "nope"\n'
        )
        paths = {i.path for i in issues_of(text)}
        self.assertTrue(
            {"version", "providers.a.project", "providers.a.location", "providers.b.type"} <= paths,
            paths,
        )

    def test_the_message_lists_each_problem_with_its_fix(self) -> None:
        try:
            load_text(
                _HEAD + '[tasks]\ncompation = "fast"\n\n' + _GROQ + _FAST, source="~/models.toml"
            )
        except ModelsConfigError as exc:
            text = str(exc)
        self.assertIn("~/models.toml has 1 problem:", text)
        self.assertIn("line 7 · tasks.compation", text)
        self.assertIn("Fix:  did you mean 'compaction'?", text)


class NotToml(unittest.TestCase):
    def test_the_line_of_a_syntax_error(self) -> None:
        issue = only_issue("version = 1\n[defaults]\nmodel = fast\n")
        self.assertEqual((issue.path, issue.line), ("", 3))
        self.assertIn("text values need quotes", issue.fix)


class FindingTheLine(unittest.TestCase):
    TEXT = (
        "version = 1\n"  # 1
        "[tasks]\n"  # 2
        '"subagent.researcher" = "x"\n'  # 3
        "[providers.aws]\n"  # 4
        'type = "bedrock"\n'  # 5
        "auth = { method = 'x' }\n"  # 6
        "[providers.aws.extra]\n"  # 7
    )

    def test_a_quoted_key_inside_a_table(self) -> None:
        self.assertEqual(line_of(self.TEXT, ("tasks", "subagent.researcher")), 3)

    def test_a_key_inside_an_inline_table_is_the_table_line(self) -> None:
        self.assertEqual(line_of(self.TEXT, ("providers", "aws", "auth", "method")), 6)

    def test_a_top_level_key(self) -> None:
        self.assertEqual(line_of(self.TEXT, ("version",)), 1)

    def test_the_deepest_table_wins(self) -> None:
        self.assertEqual(line_of(self.TEXT, ("providers", "aws", "extra")), 7)

    def test_a_key_that_is_not_there_points_at_its_table(self) -> None:
        self.assertEqual(line_of(self.TEXT, ("providers", "aws", "region")), 4)


if __name__ == "__main__":
    unittest.main()
