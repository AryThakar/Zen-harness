# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting for this
repository (**Security → Report a vulnerability**), rather than in a public issue. Include what you found, how
to reproduce it, and which commit or release you tested.

Only the latest commit on `main` and the latest release are supported.

## What Zen harness exposes

Zen harness is a personal assistant for one person on their own computer. When reviewing a change, these are
the properties it is meant to keep:

- **Loopback only.** The app listens on `127.0.0.1:8000` and has no login: anyone who can reach that port can
  use it, so it must never be bound to another address or put behind a public tunnel as it is. The Kaggle
  bridge listens on `127.0.0.1:8080`; the embedding server, when used, on `127.0.0.1:8070`.
- **The model is remote when it runs on Kaggle.** Your messages, and the parts of your files Zen reads to answer,
  go to the llama-server in your own Kaggle notebook, over the notebook's Cloudflare tunnel (HTTPS) and with its
  password (`--api-key`). The notebook makes a new random password each session unless you set the Kaggle secret
  `LLAMA_API_KEY`. Treat the address and password like a key: whoever has both can use your model.
- **Only Zen's page changes where the model runs.** `/assistant/model-link` refuses a request from another site's
  page (its Origin), tries a new address with its password before saving it, takes each as one line, and never
  sends the saved password back to the page.
- **The Windows app's window** is Zen's own (WebView2) with a profile of its own (`data\webview`): no Microsoft
  account signs in there and nothing syncs into it. It shows only Zen's page - links elsewhere open in your default
  browser - and only that page may ask it to minimize, maximize or close. Without WebView2 it is Edge's app window
  instead, also a profile of its own (`data\window`), with Edge's sync turned off.
- **Asking first.** Downloads, deleting files and changing files you added wait for your click in the chat
  (`app/tools/approval.py`); the model cannot approve its own request.
- **The code sandbox is not a security boundary.** Scripts the assistant writes run in a separate Python process,
  with a clean environment (none of your keys), the project folder as its working directory, time limits and an
  audit hook that refuses network access, other programs and files outside the project (`app/tools/sandbox.py`).
  This keeps an honest model's mistakes contained; it does not stop a deliberately hostile native library.
- **Pages Zen writes** run in the chat's sandboxed frame under a Content Security Policy that loads nothing from
  anywhere (`web/js/files.js`), and are tried headless under the same policy before they are called ready.
- **Your data stays on disk, unencrypted.** Projects, files, chats and their search index live in `data/` (the
  Windows app: `data\` next to `Zen-harness.exe`). Back it up or delete it like any folder of documents.
- **Secrets.** API keys live in `.env` (git-ignored) or the Windows app's `settings.env`; the scripts the
  sandbox runs never see them, and no tool result the model reads includes them.

The model weights, llama.cpp and the notebook's downloads come from their publishers (see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)); only use sources you trust.
