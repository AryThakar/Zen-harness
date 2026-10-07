"""Settings, read from environment variables - and from the project's .env file, so keys work
however the app is started (from a terminal, an IDE or another tool), without system variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """NAME=value lines of the .env file (git-ignored: keys stay out of the repository) into the
    environment. A variable that is already set wins; # starts a comment; quotes are optional.
    Scripts run by run_python get a clean environment of their own, so they never see these."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return  # no .env: the environment alone
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.removeprefix("export ").split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


load_env_file()  # before Settings: its defaults read the environment when the class is made


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("BIZ_DB_PATH", "data/business.duckdb")
    # How to read ambiguous dates like 03/04/2025 when a file gives no hint.
    default_date_order: str = os.environ.get("BIZ_DEFAULT_DATE_ORDER", "dayfirst")
    # Uploaded files wait here between preview and confirm.
    staging_dir: str = os.environ.get("BIZ_STAGING_DIR", "data/staging")
    max_upload_mb: int = int(os.environ.get("BIZ_MAX_UPLOAD_MB", "100"))
    # Where the Streamlit UI finds the API.
    api_url: str = os.environ.get("BIZ_API_URL", "http://localhost:8000")
    # Currency symbol the AI uses when talking about money.
    currency: str = os.environ.get("BIZ_CURRENCY", "₹")
    # The AI assistant's model. "anthropic" uses Claude through the official SDK
    # (needs ANTHROPIC_API_KEY); "llamacpp" uses a local model served by llama-server.
    llm_provider: str = os.environ.get("BIZ_LLM_PROVIDER", "anthropic")
    llm_model: str = os.environ.get("BIZ_LLM_MODEL", "claude-opus-5")
    llm_effort: str | None = os.environ.get("BIZ_LLM_EFFORT") or None  # low | medium | high | xhigh | max
    # The app's own endpoint setting, so it never picks up ANTHROPIC_BASE_URL meant for other tools.
    anthropic_base_url: str = os.environ.get("BIZ_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    # The local model (BIZ_LLM_PROVIDER=llamacpp): where llama-server listens, and the
    # name to show (llama-server serves one model, whatever the name).
    llamacpp_url: str = os.environ.get("BIZ_LLAMACPP_URL", "http://127.0.0.1:8080")
    llamacpp_model: str = os.environ.get("BIZ_LLAMACPP_MODEL", "local")  # the --alias; the UI shows the loaded file
    # Let the local model think before answering: better on hard questions, much slower on a laptop GPU.
    llm_thinking: bool = os.environ.get("BIZ_LLM_THINKING", "off").lower() in ("1", "on", "true", "yes")
    # The assistant on Groq's cloud instead (BIZ_LLM_PROVIDER=groq): for testing the app with a large model.
    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    groq_model: str = os.environ.get("BIZ_GROQ_MODEL", "openai/gpt-oss-120b")
    # Conversation memory (LangGraph checkpoints).
    chat_db_path: str = os.environ.get("BIZ_CHAT_DB_PATH", "data/chat.sqlite")
    # Document search: the embedding model (run locally by fastembed, downloaded once).
    embed_model: str = os.environ.get("BIZ_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    embed_cache_dir: str = os.environ.get("BIZ_EMBED_CACHE_DIR", "data/models")
    # The assistant (app/assistant): its workspaces, chats and their files live here. Both model
    # slots are served by the same llama-server (BIZ_LLAMACPP_URL): slot 0 talks, slot 1 runs workers.
    assistant_dir: str = os.environ.get("ASSISTANT_DATA_DIR", "data/assistant")
    assistant_thinking: bool = os.environ.get("ASSISTANT_THINKING", "on").lower() in ("1", "on", "true", "yes")
    assistant_max_tokens: int = int(os.environ.get("ASSISTANT_MAX_TOKENS", "4096"))  # per reply, thinking included
    # A cap on thinking tokens per reply; 0 = none (the default: main offers "answer quickly" instead).
    assistant_thinking_budget: int = int(os.environ.get("ASSISTANT_THINKING_BUDGET", "0"))
    # Who the assistant is and whom it talks to (its persona, app/assistant/prompts.py); no user name = "the user".
    assistant_name: str = os.environ.get("ASSISTANT_NAME", "Zen")
    assistant_user_name: str = os.environ.get("ASSISTANT_USER_NAME", "")


settings = Settings()
