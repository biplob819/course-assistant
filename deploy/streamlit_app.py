"""The hosted demo's entry point (D3.2): Streamlit Community Cloud runs this file.

    main file path: deploy/streamlit_app.py   (dependencies: deploy/requirements.txt)
    local run:      uv run --extra app --extra openai streamlit run deploy/streamlit_app.py

It fills in the demo's non-secret settings, refuses to serve (a locked page with fixed text)
when a required secret is missing, a passkey hash is malformed or the two hashes are equal,
or when a pilot course (CA_CONFIG) has no gate-approved corpus to serve (P4.1: LIVE.json
missing or unreadable; the demo cache's replay corpus needs none), and otherwise routes
the learner page (the default, opening on the worked examples when the demo cache is there)
and the teaching-team page, each behind its own passkey.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    import course_assistant  # noqa: F401  (installed in development and CI)
except ImportError:  # the host installs deploy/requirements.txt, not the package itself
    sys.path.insert(0, str(ROOT / "src"))

import streamlit as st  # noqa: E402

from course_assistant.app import deploy, wiring  # noqa: E402

deploy.apply_defaults(os.environ, ROOT)
problem = deploy.startup_problem(os.environ, deploy.LiveSecrets(lambda: st.secrets))
problem = problem or deploy.course_problem(os.environ)
problem = problem or wiring.serving_problem(os.environ)  # checked at every run (P4.1)
if problem is not None:
    deploy.log_startup_problem(problem)  # secret names only, never a value
    st.set_page_config(page_title="Course assistant")
    st.title("Course assistant")
    st.error(deploy.LOCKED)
    st.stop()

pages = [st.Page(p.path, title=p.title, url_path=p.url_path, default=p.default)
         for p in deploy.PAGES]  # fmt: skip
st.navigation(pages).run()
