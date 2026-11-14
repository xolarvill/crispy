import asyncio

import httpx
import pytest

from app.agents.runtime import AgentsRuntime
from app.main import create_app
from app.data.session import get_db


@pytest.fixture
def expand(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

    async def post(idea):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/image-prompts/expand", json={"idea": idea})

    return lambda idea: asyncio.run(post(idea))


def test_prompt_expansion_uses_text_provider(expand, monkeypatch):
    def complete(self, provider, model, prompt, runtime_config):
        assert provider and model
        assert '"idea": "雨巷，木刻版画"' in prompt
        return "纵向木刻版画，以街灯的白色刻痕描出积水纹理。", model, 0

    monkeypatch.setattr(AgentsRuntime, "_chat_complete", complete)
    response = expand("  雨巷，木刻版画 ")
    assert response.status_code == 200
    assert response.json()["prompt"].startswith("纵向木刻")


@pytest.mark.parametrize("idea", ["   ", "x" * 4001])
def test_prompt_expansion_rejects_invalid_input(expand, idea):
    assert expand(idea).status_code == 422


def test_prompt_expansion_handles_empty_model_response(expand, monkeypatch):
    monkeypatch.setattr(AgentsRuntime, "_chat_complete", lambda *args: (" ", "model", 0))
    assert expand("一只鞋刷").status_code == 502
