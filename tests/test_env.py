from finagent.env import load_dotenv, parse_dotenv


def test_parse_dotenv_handles_quotes_comments_and_export():
    text = """
# a comment
FINAGENT_LLM_MODEL=openai/gpt-4o-mini
export FINAGENT_LLM_API_KEY="sk-or-abc"
SINGLE='x y'
INLINE=value # trailing comment
HASH="a # b"
not a pair
"""
    assert parse_dotenv(text) == {
        "FINAGENT_LLM_MODEL": "openai/gpt-4o-mini",
        "FINAGENT_LLM_API_KEY": "sk-or-abc",
        "SINGLE": "x y",
        "INLINE": "value",
        "HASH": "a # b",
    }


def test_load_dotenv_does_not_override_existing_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FA_TEST_KEEP=from-file\nFA_TEST_NEW=from-file\n")
    monkeypatch.setenv("FA_TEST_KEEP", "from-shell")
    monkeypatch.delenv("FA_TEST_NEW", raising=False)
    assert load_dotenv(env) == env
    import os
    assert os.environ["FA_TEST_KEEP"] == "from-shell"
    assert os.environ["FA_TEST_NEW"] == "from-file"
    monkeypatch.delenv("FA_TEST_NEW")


def test_load_dotenv_returns_none_without_a_file(tmp_path):
    assert load_dotenv(tmp_path / "missing.env") is None


def test_automatic_lookup_can_be_disabled(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FA_TEST_AUTO=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FA_TEST_AUTO", raising=False)
    monkeypatch.setenv("FINAGENT_SKIP_DOTENV", "1")
    assert load_dotenv() is None
    monkeypatch.delenv("FINAGENT_SKIP_DOTENV")
    assert load_dotenv() == tmp_path / ".env"
    monkeypatch.delenv("FA_TEST_AUTO")
