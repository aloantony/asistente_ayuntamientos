import io
import json
from urllib.error import HTTPError

import pytest

from app.assistant import groq
from app.assistant.gateway import AIGateway, AssistantUnavailableError, AIToolUseBlock
from app.core.config import Settings, settings


class Response(io.BytesIO):
    def read1(self, size):
        return self.read(size)


def events(*choices):
    return b''.join(('data: '+json.dumps(c)+'\n\n').encode() for c in choices) + b'data: [DONE]\n\n'


def chunk(delta=None, finish=None):
    return {'choices': [{'delta': delta or {}, 'finish_reason': finish}]}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'assistant_runtime', 'groq')
    monkeypatch.setattr(settings, 'groq_api_key', 'test-secret')


def install(monkeypatch, body):
    captured = []
    response = Response(body)
    def open_request(request, *, timeout):
        captured.append(request)
        return response
    monkeypatch.setattr(groq, 'urlopen_without_redirects', open_request)
    return captured, response


def drain(stream):
    deltas = []
    while True:
        try:
            deltas.append(next(stream).text)
        except StopIteration as e:
            return deltas, e.value


def test_stream_text_usage_and_explicit_provider(monkeypatch):
    captured, response = install(monkeypatch, events(chunk({'content': 'Hola '}), chunk({'content': 'pueblo'}), chunk(finish='stop'), {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3}}))
    deltas, result = drain(AIGateway().complete_stream(system='Public test', messages=[{'role': 'user', 'content': 'Hola'}], tools=[]))
    assert deltas == ['Hola ', 'pueblo']
    assert result.content[0].text == ''.join(deltas)
    assert result.usage.input_tokens == 10
    assert result.model == settings.groq_model
    assert captured[0].full_url == groq.GROQ_URL
    payload = json.loads(captured[0].data)
    assert payload['model'] == settings.groq_model
    assert 'tools' not in payload
    assert response.closed


def test_tool_fragments_and_roundtrip(monkeypatch):
    body = events(chunk({'tool_calls': [{'index': 0, 'id': 'call1', 'function': {'name': 'lookup', 'arguments': '{"id":'}}]}), chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '7}'}}]}), chunk(finish='tool_calls'))
    install(monkeypatch, body)
    result = AIGateway().complete(system='test', messages=[], tools=[])
    assert result.content == [AIToolUseBlock('call1', 'lookup', {'id': 7})]
    assert result.stop_reason == 'tool_use'
    request = groq._request('test', [{'role': 'assistant', 'content': result.content}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call1', 'content': 'public record'}]}], [{'name': 'lookup', 'input_schema': {'type': 'object'}}], stream=True)
    payload = json.loads(request.data)
    assert payload['messages'][-1] == {'role': 'tool', 'tool_call_id': 'call1', 'content': 'public record'}
    assert payload['parallel_tool_calls'] is False


@pytest.mark.parametrize('arguments,finish', [('{bad', 'tool_calls'), ('[]', 'tool_calls'), ('{"id":1}', 'length')])
def test_invalid_or_partial_tools_fail_closed(monkeypatch, arguments, finish):
    install(monkeypatch, events(chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'write', 'arguments': arguments}}]}), chunk(finish=finish)))
    with pytest.raises(AssistantUnavailableError):
        AIGateway().complete(system='test', messages=[], tools=[])


def test_tool_looking_text_is_not_executed(monkeypatch):
    text = '<tool_call>{"name":"delete","arguments":{}}</tool_call>'
    install(monkeypatch, events(chunk({'content': text}), chunk(finish='stop')))
    result = AIGateway().complete(system='test', messages=[], tools=[])
    assert result.stop_reason == 'end_turn'
    assert result.content[0].text == text


def test_truncated_stream_fails_without_retry(monkeypatch):
    captured, response = install(monkeypatch, events(chunk({'content': 'partial'})))
    with pytest.raises(AssistantUnavailableError):
        drain(AIGateway().complete_stream(system='test', messages=[], tools=[]))
    assert len(captured) == 1
    assert response.closed


def test_quota_error_hides_content_and_never_falls_back(monkeypatch, caplog):
    calls = []
    def fail(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 429, 'private content test-secret', {}, io.BytesIO(b'private content'))
    monkeypatch.setattr(groq, 'urlopen_without_redirects', fail)
    with pytest.raises(AssistantUnavailableError, match='quota exceeded'):
        AIGateway().complete(system='test', messages=[], tools=[])
    assert len(calls) == 1
    assert 'test-secret' not in caplog.text
    assert 'private content' not in caplog.text


def test_cancel_closes_socket(monkeypatch):
    _, response = install(monkeypatch, events(chunk({'content': 'first'}), chunk(finish='stop')))
    stream = AIGateway().complete_stream(system='test', messages=[], tools=[])
    assert next(stream).text == 'first'
    stream.close()
    assert response.closed


def test_key_is_required_and_runtime_is_valid(monkeypatch):
    assert Settings(assistant_runtime='groq').assistant_runtime == 'groq'
    monkeypatch.setattr(settings, 'groq_api_key', None)
    assert not AIGateway().enabled
    with pytest.raises(AssistantUnavailableError, match='not configured'):
        AIGateway().complete(system='test', messages=[], tools=[])
