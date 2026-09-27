"""The design system: tokens, glass surfaces and a few shared components.

Base colors and fonts live in .streamlit/config.toml (native theme, per mode).
This module layers what Streamlit's theme can't express: an ambient gradient
backdrop, frosted-glass cards with a light-catching hairline edge, soft layered
shadows, a navigation rail, and refined metric tiles, tabs, buttons and chat.

Cards are keyed containers - ``card("name")`` - because Streamlit exposes a
container's key as a CSS class (``st-key-card_name``); plain bordered
containers can't be told apart from layout wrappers in CSS.
"""

from __future__ import annotations

import html

import streamlit as st

TOKENS = {
    "light": {
        "bg": "#F4F6FB",
        "glow1": "rgba(94, 125, 255, 0.20)",
        "glow2": "rgba(157, 120, 255, 0.14)",
        "glow3": "rgba(56, 189, 248, 0.10)",
        "glass": "rgba(255, 255, 255, 0.62)",
        "glass_strong": "rgba(255, 255, 255, 0.86)",
        "glass_border": "rgba(23, 33, 66, 0.08)",
        "edge_hi": "rgba(255, 255, 255, 0.95)",
        "edge_lo": "rgba(62, 99, 221, 0.10)",
        "hover": "rgba(62, 99, 221, 0.06)",
        "shadow": "0 1px 2px rgba(16, 24, 40, 0.04), 0 10px 28px -12px rgba(30, 41, 90, 0.16)",
        "shadow_hover": "0 2px 4px rgba(16, 24, 40, 0.05), 0 18px 40px -14px rgba(30, 41, 90, 0.24)",
        "text": "#141A2A",
        "text2": "#4A5468",
        "muted": "#7A8499",
        "brand": "#3E63DD",
        "brand2": "#6E56CF",
        "brand_text": "#3A5BC7",
        "brand_soft": "rgba(62, 99, 221, 0.10)",
        "brand_border": "rgba(62, 99, 221, 0.28)",
        "brand_glow": "rgba(62, 99, 221, 0.45)",
        "sidebar": "rgba(255, 255, 255, 0.55)",
        "bottom_fade": "rgba(244, 246, 251, 0.92)",
    },
    "dark": {
        "bg": "#0A0F1E",
        "glow1": "rgba(84, 114, 228, 0.30)",
        "glow2": "rgba(124, 92, 255, 0.18)",
        "glow3": "rgba(34, 211, 238, 0.08)",
        "glass": "rgba(22, 30, 52, 0.55)",
        "glass_strong": "rgba(27, 36, 62, 0.82)",
        "glass_border": "rgba(255, 255, 255, 0.07)",
        "edge_hi": "rgba(255, 255, 255, 0.16)",
        "edge_lo": "rgba(110, 139, 255, 0.14)",
        "hover": "rgba(126, 150, 255, 0.08)",
        "shadow": "0 1px 2px rgba(0, 0, 0, 0.30), 0 14px 36px -14px rgba(0, 0, 0, 0.60)",
        "shadow_hover": "0 2px 4px rgba(0, 0, 0, 0.35), 0 22px 48px -16px rgba(0, 0, 0, 0.70)",
        "text": "#E8ECF6",
        "text2": "#AEB7CB",
        "muted": "#7C869C",
        "brand": "#5472E4",
        "brand2": "#8B6CF6",
        "brand_text": "#9EB1FF",
        "brand_soft": "rgba(84, 114, 228, 0.16)",
        "brand_border": "rgba(126, 150, 255, 0.34)",
        "brand_glow": "rgba(84, 114, 228, 0.55)",
        "sidebar": "rgba(13, 19, 36, 0.62)",
        "bottom_fade": "rgba(10, 15, 30, 0.92)",
    },
}

# Everything that renders as a card: metric tiles, keyed cards, expanders, forms.
GLASS = ", ".join([
    '[data-testid="stMetric"]',
    '[class*="st-key-card_"]',
    '[data-testid="stExpander"] details',
    '[data-testid="stForm"]',
])

CSS = """
:root {{
  --bg: {bg}; --glass: {glass}; --glass-strong: {glass_strong}; --glass-border: {glass_border};
  --edge-hi: {edge_hi}; --edge-lo: {edge_lo}; --hover: {hover};
  --shadow: {shadow}; --shadow-hover: {shadow_hover};
  --text: {text}; --text-2: {text2}; --muted: {muted};
  --brand: {brand}; --brand-2: {brand2}; --brand-text: {brand_text}; --brand-soft: {brand_soft};
  --brand-border: {brand_border}; --brand-glow: {brand_glow};
}}

/* ---- canvas ---------------------------------------------------------------- */
.stApp {{
  background:
    radial-gradient(1100px 620px at -8% -12%, {glow1}, transparent 62%),
    radial-gradient(900px 540px at 108% -6%, {glow2}, transparent 58%),
    radial-gradient(1000px 700px at 45% 118%, {glow3}, transparent 60%),
    var(--bg) !important;
  background-attachment: fixed !important;
}}
header[data-testid="stHeader"] {{ background: transparent !important; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stMainBlockContainer"] {{ max-width: 1320px; padding: 3.4rem 2.6rem 5rem; }}
::selection {{ background: var(--brand-soft); }}
* {{ scrollbar-width: thin; scrollbar-color: var(--glass-border) transparent; }}

/* ---- type ------------------------------------------------------------------ */
[data-testid="stMainBlockContainer"] h2 {{
  font-size: 2.05rem !important; font-weight: 750 !important; letter-spacing: -0.028em; line-height: 1.15;
  padding-top: 0.1rem !important;
}}
[data-testid="stMainBlockContainer"] h3 {{
  font-size: 1.06rem !important; font-weight: 650 !important; letter-spacing: -0.012em;
}}
[data-testid="stHeaderActionElements"] {{ display: none; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: var(--text-2) !important; }}
code, pre {{ font-feature-settings: "calt" 0; }}
.bi-eyebrow {{
  font-size: 0.74rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--brand-text); margin-bottom: -0.35rem;
}}
.bi-sub {{ color: var(--text-2); font-size: 0.98rem; max-width: 760px; margin-top: -0.2rem; }}

/* ---- glass surfaces -------------------------------------------------------- */
{glass_selector} {{
  position: relative;
  background: var(--glass) !important;
  -webkit-backdrop-filter: blur(18px) saturate(150%); backdrop-filter: blur(18px) saturate(150%);
  border: 1px solid var(--glass-border) !important;
  border-radius: 18px !important;
  box-shadow: var(--shadow);
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}}
/* the light-catching edge: a gradient hairline, bright at the top-left */
{glass_before} {{
  content: ""; position: absolute; inset: 0; border-radius: inherit; padding: 1px; pointer-events: none;
  background: linear-gradient(160deg, var(--edge-hi), transparent 38%, transparent 70%, var(--edge-lo));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude;
}}
[class*="st-key-card_"] {{ padding: 1.15rem 1.3rem 1.05rem !important; }}
[data-testid="stMetric"]:hover, [class*="st-key-card_"]:hover {{ box-shadow: var(--shadow-hover); }}

/* ---- metric tiles ---------------------------------------------------------- */
[data-testid="stMetric"] {{ padding: 1rem 1.15rem 0.9rem !important; }}
[data-testid="stMetricLabel"] p {{
  font-size: 0.72rem !important; font-weight: 700 !important; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text-2) !important;
}}
[data-testid="stMetric"] svg.marks {{ background: transparent !important; }}
[data-testid="stMetric"] svg.marks > rect.background, [data-testid="stMetric"] svg.marks path.background {{ fill: transparent !important; }}
[data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; letter-spacing: -0.025em; color: var(--text); }}
[data-testid="stMetricDelta"] {{
  width: fit-content; padding: 0.1rem 0.55rem 0.1rem 0.4rem; border-radius: 999px; font-weight: 650;
  background: color-mix(in srgb, currentColor 12%, transparent);
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
}}

/* ---- navigation rail ------------------------------------------------------- */
[data-testid="stSidebar"] {{
  background: {sidebar} !important;
  -webkit-backdrop-filter: blur(26px) saturate(160%); backdrop-filter: blur(26px) saturate(160%);
  border-right: 1px solid var(--glass-border);
}}
[data-testid="stSidebar"] > div {{ background: transparent !important; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0.4rem; }}
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has([data-testid="stRadio"]),
[data-testid="stSidebar"] [data-testid="stRadio"], [data-testid="stSidebar"] [data-testid="stRadioGroup"],
[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div {{ width: 100% !important; }}
[data-testid="stSidebar"] [data-testid="stRadioGroup"] {{ gap: 3px; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"] {{
  width: 100%; margin: 0; padding: 0.62rem 0.8rem; border-radius: 12px; border: 1px solid transparent;
  transition: background 0.15s ease, border-color 0.15s ease;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) {{ display: none; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"] p {{ font-weight: 560; color: var(--text-2); font-size: 0.95rem; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"] span[role="img"] {{
  color: var(--muted); margin-right: 0.6rem; font-size: 1.25rem !important; vertical-align: -0.28rem !important;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {{ background: var(--hover); }}
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {{
  background: var(--glass-strong); border-color: var(--glass-border); box-shadow: var(--shadow);
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] p {{ color: var(--text); font-weight: 650; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] span[role="img"] {{
  color: var(--brand-text);
}}
.bi-brand {{ display: flex; align-items: center; gap: 0.7rem; padding: 0.2rem 0.3rem 1.1rem; }}
.bi-brand-mark {{
  width: 38px; height: 38px; border-radius: 12px; display: flex; align-items: flex-end; justify-content: center;
  gap: 3px; padding-bottom: 9px; flex: none;
  background: linear-gradient(140deg, var(--brand), var(--brand-2));
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.35) inset, 0 8px 18px -6px var(--brand-glow);
}}
.bi-brand-mark i {{ display: block; width: 4.5px; border-radius: 2px; background: #fff; }}
.bi-brand-mark i:nth-child(1) {{ height: 8px; opacity: 0.7; }}
.bi-brand-mark i:nth-child(2) {{ height: 13px; opacity: 0.85; }}
.bi-brand-mark i:nth-child(3) {{ height: 18px; }}
.bi-brand-name {{ font-weight: 750; font-size: 1.02rem; letter-spacing: -0.015em; color: var(--text); line-height: 1.2; }}
.bi-brand-tag {{ font-size: 0.76rem; color: var(--muted); font-weight: 550; }}
.bi-status {{
  margin-top: 1.1rem; padding: 0.75rem 0.85rem; border-radius: 14px; font-size: 0.82rem; color: var(--text-2);
  background: var(--glass); border: 1px solid var(--glass-border); line-height: 1.45;
}}
.bi-status b {{ color: var(--text); font-weight: 650; font-variant-numeric: tabular-nums; }}
.bi-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }}

/* ---- buttons --------------------------------------------------------------- */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {{
  background: linear-gradient(135deg, var(--brand), var(--brand-2)) !important;
  border: 1px solid rgba(255, 255, 255, 0.16) !important; color: #fff !important; font-weight: 650;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.28) inset, 0 10px 22px -10px var(--brand-glow);
  transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease;
}}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {{
  transform: translateY(-1px); filter: brightness(1.06);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.3) inset, 0 16px 30px -12px var(--brand-glow);
}}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {{
  background: var(--glass-strong) !important; border: 1px solid var(--glass-border) !important;
  font-weight: 600; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
}}
[data-testid="stBaseButton-secondary"]:hover {{ border-color: var(--brand-border) !important; color: var(--brand-text) !important; }}

/* ---- tabs and segmented controls ------------------------------------------ */
[data-testid="stTabs"] [role="tablist"] {{
  gap: 2px; padding: 4px; width: fit-content; max-width: 100%; border-radius: 14px;
  background: var(--glass); border: 1px solid var(--glass-border);
  -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
}}
[data-testid="stTab"] {{ height: 2.2rem; padding: 0 0.95rem !important; border-radius: 10px; }}
[data-testid="stTab"] p {{ font-weight: 600; color: var(--text-2); }}
[data-testid="stTab"][aria-selected="true"] {{ background: var(--glass-strong); box-shadow: var(--shadow); }}
[data-testid="stTab"][aria-selected="true"] p {{ color: var(--text); }}
[data-testid="stTab"] .react-aria-SelectionIndicator {{ display: none; }}
[data-testid="stButtonGroup"] button {{ font-weight: 600; }}

/* ---- inputs ---------------------------------------------------------------- */
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"], [data-testid="stDateInput"] > div,
[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
  background: var(--glass-strong) !important; border-radius: 12px !important;
}}
[data-testid="stFileUploaderDropzone"] {{
  background: var(--glass) !important; border: 1.5px dashed var(--brand-border) !important; border-radius: 14px !important;
}}

/* ---- alerts, tables, code -------------------------------------------------- */
[data-testid="stAlertContainer"] {{
  border-radius: 14px !important; border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
}}
[data-testid="stDataFrame"] {{ border-radius: 14px; overflow: hidden; }}
[data-testid="stCode"] pre {{ border-radius: 12px !important; border: 1px solid var(--glass-border); }}
[data-testid="stExpander"] summary p {{ font-weight: 600; }}
[data-testid="stExpanderDetails"] {{ border: none !important; }}

/* ---- chat ------------------------------------------------------------------ */
[data-testid="stChatMessage"] {{ background: transparent !important; padding: 0.35rem 0 !important; gap: 0.85rem; }}
/* user and assistant are told apart by the message's aria-label: custom avatars share one test id */
[data-testid="stChatMessage"] > div:first-child {{ width: 34px; height: 34px; border-radius: 11px; border: none; }}
[data-testid="stChatMessage"]:has([aria-label="Chat message from assistant"]) > div:first-child {{
  background: linear-gradient(140deg, var(--brand), var(--brand-2)) !important; color: #fff !important;
  box-shadow: 0 8px 16px -8px var(--brand-glow);
}}
[data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) > div:first-child {{
  background: var(--glass-strong) !important; border: 1px solid var(--glass-border); color: var(--text-2) !important;
}}
[data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) {{ flex-direction: row-reverse; justify-content: flex-start; }}
[aria-label="Chat message from user"] {{
  flex: 0 1 auto !important; width: fit-content !important; max-width: 78%; padding: 0.6rem 1.05rem;
  margin: 0 !important;
  border-radius: 16px 5px 16px 16px; background: var(--brand-soft); border: 1px solid var(--brand-border);
}}
[aria-label="Chat message from assistant"] {{
  padding: 0.95rem 1.2rem; border-radius: 5px 18px 18px 18px;
  background: var(--glass); border: 1px solid var(--glass-border); box-shadow: var(--shadow);
  -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px);
}}
[data-testid="stBottom"] > div {{ background: linear-gradient(to top, {bottom_fade} 60%, transparent) !important; }}
[data-testid="stChatInput"] > div {{
  background: var(--glass-strong) !important; border: 1px solid var(--glass-border) !important; border-radius: 18px !important;
  box-shadow: var(--shadow); -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px);
}}
[data-testid="stChatInput"] > div:focus-within {{ border-color: var(--brand-border) !important; box-shadow: 0 0 0 4px var(--brand-soft), var(--shadow); }}
[data-testid="stChatInputSubmitButton"] {{ background: linear-gradient(135deg, var(--brand), var(--brand-2)) !important; color: #fff !important; border-radius: 12px !important; }}

.st-key-suggestions {{ gap: 0.5rem !important; margin-top: 0.4rem; }}
.st-key-suggestions button {{
  border-radius: 999px !important; padding: 0.4rem 1rem !important; min-height: 0 !important;
  font-weight: 560 !important;
}}

/* ---- hero / briefing ------------------------------------------------------- */
.bi-hero {{ font-size: 1.28rem; line-height: 1.55; font-weight: 500; color: var(--text-2); letter-spacing: -0.012em; }}
.bi-hero b {{ color: var(--text); font-weight: 700; }}
.bi-hero .up {{ color: #1F9D55; font-weight: 700; }}
.bi-hero .down {{ color: #D93D42; font-weight: 700; }}
.bi-chip {{
  display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.22rem 0.65rem; border-radius: 999px;
  font-size: 0.78rem; font-weight: 650; color: var(--brand-text); background: var(--brand-soft);
  border: 1px solid var(--brand-border);
}}
"""

LOGO = "<i></i><i></i><i></i>"  # three rising bars, drawn in CSS (st.html strips SVG)


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:
        return "light"


def inject() -> None:
    """Add the design system's CSS for the current light/dark mode."""
    tokens = TOKENS[mode()]
    before = ", ".join(f"{s}::before" for s in GLASS.split(", "))
    st.html("<style>" + CSS.format(glass_selector=GLASS, glass_before=before, **tokens) + "</style>")


def brand() -> None:
    st.html(
        f'<div class="bi-brand"><div class="bi-brand-mark">{LOGO}</div>'
        '<div><div class="bi-brand-name">AI Business Analyst</div>'
        '<div class="bi-brand-tag">Sales intelligence</div></div></div>'
    )


def status(rows: int, uploads: int, model: str | None = None, ok: bool = True) -> None:
    dot = "#1F9D55" if ok else "#D93D42"
    lines = [f'<span class="bi-dot" style="background:{dot}"></span><b>{rows:,}</b> sales rows · '
             f'<b>{uploads:,}</b> upload{"s" if uploads != 1 else ""}']
    if model:
        lines.append(f"Assistant: {html.escape(model)}")
    st.html('<div class="bi-status">' + "<br>".join(lines) + "</div>")


def page_header(title: str, subtitle: str | None = None, eyebrow: str | None = None) -> None:
    """Eyebrow, title (a real st.header, so tests and screen readers find it) and subtitle."""
    if eyebrow:
        st.html(f'<div class="bi-eyebrow">{html.escape(eyebrow)}</div>')
    st.header(title)
    if subtitle:
        st.html(f'<div class="bi-sub">{html.escape(subtitle)}</div>')


def card(name: str, stretch: bool = False):
    """A glass card: ``with card("trend"): ...``. ``stretch`` makes cards in one
    row of columns as tall as the tallest."""
    return st.container(border=True, key=f"card_{name}", height="stretch" if stretch else "content")
