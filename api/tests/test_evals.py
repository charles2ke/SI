import json

import pytest

from si.evals import evaluate, regex_match, score
from si.models import ChatResponse


class Judge:
    async def chat(self, request):
        assert request.response_schema
        return ChatResponse(model="mock", content='{"score": 0.75, "reason": "mostly correct"}')


@pytest.mark.parametrize("scorer,actual,expected,value", [
    ("exact_match", "same", "same", 1),
    ("exact_match", "same ", "same", 0),
    ("contains", "hello world", "world", 1),
    ("contains", "hello", "world", 0),
    ("regex", "value: 123", r"\d+", 1),
    ("json_schema", '{"count":2}', {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}, 1),
    ("json_schema", '{"count":"bad"}', {"type": "object", "properties": {"count": {"type": "integer"}}}, 0),
    ("json_schema", "not json", {"type": "object"}, 0),
])
async def test_scorers(scorer, actual, expected, value):
    assert await score(scorer, actual, expected) == value


async def test_llm_judge_structured():
    assert await score("llm_judge", "actual", "expected", gateway=Judge()) == 0.75


async def test_llm_judge_rejects_bad_score():
    class InvalidJudge:
        async def chat(self, request):
            return ChatResponse(model="mock", content='{"score": 5, "reason": "bad"}')

    with pytest.raises(Exception):
        await score("llm_judge", "actual", "expected", gateway=InvalidJudge())


async def test_regex_deadline():
    with pytest.raises(ValueError, match="timed out"):
        await regex_match("(a+)+$", "a" * 10000 + "!")
    with pytest.raises(ValueError, match="limits"):
        await regex_match("x", "x" * 20001)


async def test_schema_cannot_fetch_remote_references():
    with pytest.raises(ValueError, match="references"):
        await score("json_schema", "{}", {"$ref": "http://127.0.0.1/secret"})
    with pytest.raises(ValueError, match="regex"):
        await score("json_schema", '"aaa"', {"type": "string", "pattern": "(a+)+$"})


async def test_evaluation_persists_individual_results(database):
    with database() as session:
        result = await evaluate(
            session, "test-one",
            [
                {"input": "first", "expected": "yes", "output": "yes"},
                {"input": "second", "expected": "yes", "output": "no"},
            ],
            "exact_match", gateway=Judge(),
        )
        assert result.status == "completed"
        assert result.score == 0.5
        assert len(result.results) == 2
        assert result.results[0]["passed"] and not result.results[1]["passed"]
        session.expire_all()
        assert json.loads(json.dumps(result.results))[0]["output"] == "yes"


async def test_evaluation_accepts_api_config_and_item_errors(database):
    with database() as session:
        result = await evaluate(
            session, "test-one",
            {"dataset": [{"input": "bad regex", "output": "x", "expected": "["}], "scorer": "regex"},
            gateway=Judge(),
        )
        assert result.score == 0
        assert result.results[0]["error"]


async def test_evaluation_tenant_and_prompt_scope(database):
    with database() as session:
        with pytest.raises(ValueError, match="project"):
            await evaluate(session, "test-one", [{"input": "x", "expected": "x"}], agent_id="missing")
        with pytest.raises(ValueError, match="project"):
            await evaluate(session, "test-one", [{"input": "x", "expected": "x"}], prompt_name="missing")


def test_eval_http_api_dataset(client, redis):
    prompt = client.post("/v1/prompts", json={"name": "echo", "template": "{input}"})
    assert prompt.status_code == 201, prompt.text
    response = client.post(
        "/v1/evals",
        json={
            "dataset": [{"input": "hello", "expected": "Mock response: hello"}],
            "scorer": "exact_match", "prompt_name": "echo", "prompt_version": 1,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["score"] == 1
    assert redis.usage[0] > 0
