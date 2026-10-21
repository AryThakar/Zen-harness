# Third-party notices

Zen harness's own source code is copyright AryThakar and licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE). It builds on the work below, which remains under its
authors' licenses and is not affected by Zen harness's license.

## In this repository

| Component | Use in Zen harness | License |
| --- | --- | --- |
| [Google Sans Flex](https://fonts.google.com/specimen/Google+Sans+Flex) and [Google Sans Code](https://fonts.google.com/specimen/Google+Sans+Code) typefaces | The interface's type, `web/fonts/` | SIL Open Font License 1.1 - full texts in [`web/fonts/OFL-google-sans-flex.txt`](web/fonts/OFL-google-sans-flex.txt) and [`web/fonts/OFL-google-sans-code.txt`](web/fonts/OFL-google-sans-code.txt) |

The startup sound (`web/sounds/startup.ogg`) is AryThakar's own, from [Zen](https://github.com/AryThakar/zen); the
sample tables in `data/samples/` are hand-made for this project, every name and number in them made up.

## Loaded by the page from a CDN

Not included in this repository or its releases: the browser fetches them from jsDelivr or cdnjs, pinned
to the versions below.

| Component | Use in Zen harness | License |
| --- | --- | --- |
| [marked](https://github.com/markedjs/marked) 15.0.12 | Markdown in replies | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.16 | Cleaning the HTML Markdown makes | Apache-2.0 or MPL-2.0 |
| [KaTeX](https://github.com/KaTeX/KaTeX) 0.16.47 | Maths | MIT |
| [highlight.js](https://github.com/highlightjs/highlight.js) 11.9.0 | Code colouring | BSD-3-Clause |
| [Chart.js](https://github.com/chartjs/Chart.js) 4.5.1 | Live charts | MIT |
| [Mermaid](https://github.com/mermaid-js/mermaid) 11.17.2 | Diagrams | MIT |
| [PDF.js](https://github.com/mozilla/pdf.js) 4.10.38 | The PDF viewer | Apache-2.0 |

## Redistributed in the Windows release

`Zen-harness-<version>-windows-x64.zip` on the [releases page](https://github.com/AryThakar/Zen-harness/releases)
carries, unmodified:

| Component | Use in Zen harness | License |
| --- | --- | --- |
| [Python](https://www.python.org/) 3.13 | Runs the app and the scripts the code sandbox runs, `runtime\` | Python Software Foundation License - full text in `runtime\LICENSE.txt` |
| [Node.js](https://nodejs.org/) 22 (`node.exe`) | Runs Playwright MCP, `tools\node\` | MIT, with the licenses of the code it bundles |
| [Playwright](https://github.com/microsoft/playwright) and [Playwright MCP](https://github.com/microsoft/playwright-mcp) | The browser Zen reads blocked pages with and tries its games in, `tools\playwright-mcp\` | Apache-2.0 |
| The Python libraries below, in `runtime\Lib\site-packages\` | Data, charts, forecasts, files, the web, the server | Each library's own license (as its metadata gives it, below); every one's license files are in its `.dist-info` folder |

| Library | Version | License |
| --- | --- | --- |
| adagio | 0.2.6 | Apache Software License |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| beautifulsoup4 | 4.15.0 | MIT License |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cloudpickle | 3.1.2 | BSD License |
| colorama | 0.4.6 | BSD License |
| contourpy | 1.4.0 | BSD-3-Clause |
| coreforecast | 0.0.18 | Apache Software License |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| cycler | 0.12.1 | BSD License |
| ddgs | 9.16.0 | MIT |
| defusedxml | 0.7.1 | Python Software Foundation License |
| distro | 1.9.0 | Apache Software License |
| duckdb | 1.5.5 | MIT License |
| et_xmlfile | 2.0.0 | MIT License |
| fastapi | 0.141.1 | MIT |
| fastembed | 0.8.1 | Apache Software License |
| filelock | 4.0.4 | MIT |
| flatbuffers | 25.12.19 | Apache Software License |
| fonttools | 4.66.1 | MIT |
| formulaic | 1.2.2 | MIT |
| fsspec | 2026.9.0 | BSD-3-Clause |
| fugue | 0.9.7 | Apache-2.0 |
| h11 | 0.16.0 | MIT License |
| hf-xet | 1.6.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| httpx2 | 2.13.1 | BSD-3-Clause |
| huggingface_hub | 1.33.0 | Apache Software License |
| idna | 3.20 | BSD-3-Clause |
| ImageIO | 2.38.0 | BSD-2-Clause |
| imbalanced-learn | 0.14.2 | MIT License |
| interface_meta | 2.0.1 | MIT |
| Jinja2 | 3.1.6 | BSD License |
| joblib | 1.6.0 | BSD-3-Clause |
| jsonpatch | 1.33 | BSD License |
| jsonpointer | 3.1.1 | BSD License |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| kiwisolver | 1.5.1 | BSD License |
| langchain-core | 1.6.5 | MIT License |
| langchain-protocol | 0.0.19 | MIT License |
| langgraph | 1.2.12 | MIT |
| langgraph-checkpoint | 4.2.0 | MIT |
| langgraph-prebuilt | 1.1.0 | MIT |
| langgraph-sdk | 0.4.5 | MIT |
| langsmith | 0.14.1 | MIT |
| lazy-loader | 0.6 | BSD-3-Clause |
| lightgbm | 4.7.0 | MIT |
| loguru | 0.7.3 | MIT License |
| lxml | 6.1.3 | BSD-3-Clause |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| matplotlib | 3.11.2 | Python Software Foundation License |
| mcp | 2.3.0 | MIT License |
| mcp-types | 2.3.0 | MIT License |
| mmh3 | 5.3.0 | MIT License |
| mpmath | 1.3.0 | BSD License |
| narwhals | 2.26.0 | MIT |
| networkx | 3.7 | BSD-3-Clause |
| nltk | 3.10.3 | Apache Software License |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.30.0 | MIT License |
| opencv-python-headless | 5.0.0.93 | Apache Software License |
| openpyxl | 3.1.5 | MIT License |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pandas | 2.3.3 | BSD License |
| patsy | 1.0.3 | BSD License |
| pillow | 12.3.0 | MIT-CMU |
| polars | 1.44.2 | MIT License |
| polars-runtime-32 | 1.44.2 | MIT License |
| primp | 2.0.1 | MIT License |
| protobuf | 7.36.2 | 3-Clause BSD License |
| py_rust_stemmers | 0.1.8 | MIT |
| pyarrow | 25.0.1 | Apache-2.0 |
| pycparser | 3.1 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| PyJWT | 2.15.1 | MIT |
| pyparsing | 3.3.3 | MIT |
| pypdf | 6.19.0 | BSD-3-Clause |
| python-dateutil | 2.9.0.post0 | BSD License, Apache Software License |
| python-docx | 1.2.0 | MIT License |
| python-multipart | 0.0.32 | Apache-2.0 |
| python-pptx | 1.0.2 | MIT License |
| pytz | 2026.4 | MIT License |
| pywin32 | 312 | Python Software Foundation License |
| PyYAML | 6.0.3 | MIT License |
| RapidFuzz | 3.14.6 | MIT |
| referencing | 0.37.0 | MIT |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache Software License |
| requests-toolbelt | 1.0.0 | Apache Software License |
| rpds-py | 2026.6.3 | MIT |
| scikit-image | 0.26.0 | BSD License |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| scipy | 1.18.1 | BSD License |
| seaborn | 0.13.2 | BSD License |
| simpleeval | 1.0.8 | MIT License |
| six | 1.17.0 | MIT License |
| sklearn-compat | 0.1.6 | BSD License |
| sniffio | 1.3.1 | MIT License, Apache Software License |
| soupsieve | 2.10 | MIT |
| sqlglot | 30.20.0 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| statsforecast | 2.1.1 | Apache Software License |
| statsmodels | 0.15.0 | BSD-3-Clause |
| sympy | 1.14.0 | BSD License |
| tabulate | 0.10.0 | MIT |
| tenacity | 9.1.4 | Apache Software License |
| threadpoolctl | 3.7.0 | BSD-3-Clause |
| tifffile | 2026.9.20 | BSD-3-Clause |
| tokenizers | 0.23.2 | Apache Software License |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| triad | 1.0.2 | Apache-2.0 |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.4 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| utilsforecast | 0.2.16 | Apache Software License |
| uuid_utils | 0.17.1 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websockets | 16.1.1 | BSD-3-Clause |
| win32_setctime | 1.2.0 | MIT License |
| wrapt | 2.5.0 | BSD-2-Clause |
| xgboost | 3.4.1 | Apache-2.0 |
| xlrd | 2.0.2 | BSD License |
| xlsxwriter | 3.2.9 | BSD License |
| xxhash | 4.0.1 | BSD-2-Clause |
| zstandard | 0.25.0 | BSD-3-Clause |

## Obtained separately

None of these are in this repository or its releases.

| Component | Use in Zen harness | License (as published by its authors) |
| --- | --- | --- |
| Qwen3.6-35B-A3B, as [Unsloth's GGUF](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) (`UD-Q4_K_S`, `mmproj-F16`) | The language model, downloaded by the Kaggle notebook | Apache-2.0, as listed on the model page |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) | Serves the model, downloaded by the notebook | MIT |
| [cloudflared](https://github.com/cloudflare/cloudflared) | The notebook's tunnel | Apache-2.0 |
| EmbeddingGemma 2 (optional), as [ggml-org's GGUF](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF) (`Q8_0` and its `mmproj`) | Search by meaning | [Gemma Terms of Use](https://ai.google.dev/gemma/terms) |
| [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), as [Qdrant's ONNX copy](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q) (through fastembed) | Search by meaning without EmbeddingGemma, downloaded once | MIT |
| Microsoft Edge | The window Zen-harness.exe opens, and the browser Playwright drives | Part of Windows |
| [Pollinations](https://pollinations.ai/) (optional, with your own key) | Pictures | A web service, under its own terms |
