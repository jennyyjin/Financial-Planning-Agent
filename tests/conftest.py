"""Tests never read a developer's .env (it may hold a funded API key): the CLIs' automatic lookup is disabled."""

import os

os.environ["FINAGENT_SKIP_DOTENV"] = "1"
