# The model on Kaggle

Zen thinks with **Qwen 3.6 35B-A3B** and searches by meaning with **EmbeddingGemma 2**. With no big GPU of your
own, Kaggle runs both free on two T4s: this notebook starts a llama-server for each there and opens one
password-protected address that Zen on your PC talks to - your PC then runs no model at all.

| File | What it is |
|---|---|
| [`zen-qwen36-server.ipynb`](zen-qwen36-server.ipynb) | The notebook: llama.cpp's Linux CUDA builds, the models (about 22 GB), a small router (`/embed/...` to the search model, the rest to Qwen) and a Cloudflare quick tunnel |

## Start it

1. On [kaggle.com](https://www.kaggle.com/), *Create → New Notebook*, then *File → Import notebook* and pick
   `zen-qwen36-server.ipynb`.
2. In the right-hand panel: *Accelerator* `GPU T4 x2`, *Internet* `On` (Kaggle asks for a phone-verified account),
   *Persistence* `No persistence`.
3. *Run All*. After a few minutes the last cell prints a **Zen code**: the address and password in one line.
4. In Zen, paste it in **Model settings** → **Kaggle** (they open by themselves the first time).
5. **A new session while Zen runs:** click the dot by a chat's name, choose **Kaggle** and paste the new code. Zen tries
   it first, then saves it; the chat carries on.

Keep the notebook's tab open while you use Zen. A session lasts up to 12 hours, and Kaggle gives about 30 GPU hours
a week. Each session gets a new address; with the Kaggle secret `LLAMA_API_KEY` set (*Add-ons → Secrets*), the
password stays the same.

**Search.** Zen finds the search model by itself. Your files' search vectors are kept on your PC; the model only
makes them, and turns each search into one. While the notebook is off, Zen searches by keywords, and files added
meanwhile get their vectors once it is back. An older copy of this notebook (Qwen only) still works: Zen then searches
by keywords.

## Privacy

Your messages, and the parts of your files Zen reads to answer, go to your own notebook over HTTPS - and for search,
your files' text and pictures, and what you search for. Nothing is kept there after the session ends. Treat the
Zen code (or the address and password) like a key: whoever has it can use your models.
