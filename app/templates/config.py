from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.i18n import current_locale, i18n_version

TEMPLATES_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["legacy_locale"] = current_locale
templates.env.globals["legacy_i18n_version"] = i18n_version


def t(english: str, chinese: str) -> str:
    return chinese if current_locale() == "zh" else english


templates.env.globals["t"] = t
