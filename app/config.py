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
    """The assistant's settings. The variable names are the ones the .env file already uses (some keep the
    BIZ_ prefix of the first product); the legacy product reads its own (legacy/config.py)."""
    max_upload_mb: int = int(os.environ.get("BIZ_MAX_UPLOAD_MB", "100"))
    # The model behind both slots: "llamacpp" (llama-server - local, or the Kaggle bridge) or "groq" (for tests).
    llm_provider: str = os.environ.get("BIZ_LLM_PROVIDER", "llamacpp")
    # Where llama-server listens, and the name to show (llama-server serves one model, whatever the name).
    llamacpp_url: str = os.environ.get("BIZ_LLAMACPP_URL", "http://127.0.0.1:8080")
    llamacpp_model: str = os.environ.get("BIZ_LLAMACPP_MODEL", "local")  # the --alias; the UI shows the loaded file
    # The assistant on Groq's cloud instead (BIZ_LLM_PROVIDER=groq): for testing the app with a large model.
    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    groq_model: str = os.environ.get("BIZ_GROQ_MODEL", "openai/gpt-oss-120b")
    # A real browser for pages a plain read can't get (app/tools/browser.py): the folder Playwright MCP is
    # installed in (npm install @playwright/mcp, run by node) or its cli.js, and the installed browser it drives.
    # Empty: no browser.
    playwright_mcp: str = os.environ.get("ASSISTANT_PLAYWRIGHT_MCP", "")
    browser_channel: str = os.environ.get("ASSISTANT_BROWSER", "msedge")
    # Search by meaning (files, chats and pictures): EmbeddingGemma 2 served by llama-server
    # (app/rag/embed_server.py) - the model file (its GGUF), its picture projector, where the server listens,
    # and the llama-server the app starts itself (empty: one started by hand at that address is used).
    # No model file: fastembed's small CPU model (text only, downloaded once).
    embed_gguf: str = os.environ.get("ASSISTANT_EMBED_GGUF", "")
    embed_mmproj: str = os.environ.get("ASSISTANT_EMBED_MMPROJ", "")
    embed_url: str = os.environ.get("ASSISTANT_EMBED_URL", "http://127.0.0.1:8070")
    embed_server: str = os.environ.get("ASSISTANT_EMBED_SERVER", "")
    embed_model: str = os.environ.get("BIZ_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    embed_cache_dir: str = os.environ.get("BIZ_EMBED_CACHE_DIR", "data/models")
    # Workspaces, chats and their files live here. Both model slots are served by the same llama-server
    # (BIZ_LLAMACPP_URL): slot 0 talks, slot 1 runs the helpers.
    assistant_dir: str = os.environ.get("ASSISTANT_DATA_DIR", "data/assistant")
    assistant_thinking: bool = os.environ.get("ASSISTANT_THINKING", "on").lower() in ("1", "on", "true", "yes")
    assistant_max_tokens: int = int(os.environ.get("ASSISTANT_MAX_TOKENS", "4096"))  # per reply, thinking included
    # A cap on thinking tokens per reply; 0 = none (the default: main offers "answer quickly" instead).
    assistant_thinking_budget: int = int(os.environ.get("ASSISTANT_THINKING_BUDGET", "0"))
    # Who the assistant is and whom it talks to (its persona, app/assistant/prompts.py); no user name = "the user".
    assistant_name: str = os.environ.get("ASSISTANT_NAME", "Zen")
    assistant_user_name: str = os.environ.get("ASSISTANT_USER_NAME", "")

settings = Settings()
