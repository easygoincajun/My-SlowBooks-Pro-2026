"""The model picker lists what the provider offers (#254, 2.22.2).

Every provider shipped with a built-in model list, and the lists went
stale: xAI retired every model the Grok picker offered (2026-05-15), and
the Claude picker offered a legacy Haiku in a release named "Claude's
current models". Now there is no built-in list and no built-in default.
`GET /api/analytics/ai-models` asks the provider's own models endpoint with
the saved key; `POST` asks with what the Settings page holds (a provider
picked, a key typed and not yet saved — gate round 1, NEW-56 / W-4), storing
nothing. The page fills the picker from that; a company chooses, and every
AI call needs a chosen model.
"""

import json
from unittest.mock import MagicMock

import pytest

from app.services import ai_service
from app.services.ai_service import (
    PROVIDERS,
    AIProviderError,
    build_models_request,
    list_models,
    parse_models,
    provider_list,
)

ANT_KEY = "sk-ant-api03-test-0000000000000000"


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _client(*responses):
    c = MagicMock()
    c.request = MagicMock(side_effect=list(responses))
    return c


# ── no built-in list, no built-in default ──────────────────────────────────


def test_no_provider_ships_a_model_list_or_a_default():
    for spec in PROVIDERS.values():
        assert not hasattr(spec, "model_choices")
        assert not hasattr(spec, "default_model")
    for p in provider_list():
        assert "model_choices" not in p and "default_model" not in p


# ── the request each provider gets ─────────────────────────────────────────


@pytest.mark.parametrize(
    "provider, url, header",
    [
        ("anthropic", "https://api.anthropic.com/v1/models?limit=1000", "x-api-key"),
        ("grok", "https://api.x.ai/v1/language-models", "Authorization"),
        ("groq", "https://api.groq.com/openai/v1/models", "Authorization"),
        ("openai", "https://api.openai.com/v1/models", "Authorization"),
        (
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            "x-goog-api-key",
        ),
    ],
)
def test_the_list_request_goes_to_the_providers_own_endpoint(provider, url, header):
    req = build_models_request(provider, "key-123")
    assert req["method"] == "GET" and req["url"] == url
    assert "key-123" in req["headers"][header]
    assert (
        "key-123" not in req["url"]
    )  # Gemini's chat URL carries the key; its list doesn't
    # the same allowlist every other call passes
    assert ai_service._check_outbound_url(provider, req["url"]) == req["url"]


def test_cloudflare_lists_its_text_generation_models_for_the_account():
    acct = "a" * 32
    req = build_models_request("cloudflare", "tok", account_id=acct)
    assert req["url"] == (
        f"https://api.cloudflare.com/client/v4/accounts/{acct}"
        "/ai/models/search?per_page=100&task=Text%20Generation"
    )
    with pytest.raises(ValueError):
        build_models_request("cloudflare", "tok", account_id="not-hex")


@pytest.mark.parametrize(
    "provider, kw, base",
    [
        ("custom", "endpoint_url", "https://api.example.com/v1"),
        ("custom", "endpoint_url", "https://api.example.com/v1/chat/completions"),
        (
            "cloudflare_worker",
            "worker_url",
            "https://x.example.workers.dev/v1/chat/completions",
        ),
    ],
)
def test_a_gateway_is_asked_at_models_beside_chat_completions(provider, kw, base):
    req = build_models_request(provider, "tok", **{kw: base})
    assert req["url"].endswith("/v1/models") and "/chat/completions" not in req["url"]
    with pytest.raises(ValueError):
        build_models_request(provider, "tok", **{kw: "https://127.0.0.1/v1"})


# ── what each reply becomes ────────────────────────────────────────────────


def test_anthropic_active_models_newest_first_with_display_names():
    body = {
        "data": [
            {
                "id": "claude-opus-5-5",
                "display_name": "Claude Opus 5.5",
                "lifecycle": "active",
            },
            {"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"},
            {
                "id": "claude-sonnet-4-6",
                "display_name": "Claude Sonnet 4.6",
                "lifecycle": "deprecated",
            },
            {
                "id": "claude-3-opus",
                "display_name": "Claude 3 Opus",
                "lifecycle": "retired",
            },
        ]
    }
    assert parse_models("anthropic", body) == [
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
        {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    ]


def test_openai_leaves_out_the_models_that_do_not_chat_and_sorts_newest_first():
    body = {
        "data": [
            {"id": "gpt-4o-mini", "created": 1_700_000_000},
            {"id": "text-embedding-3-large", "created": 1_800_000_000},
            {"id": "whisper-1", "created": 1_800_000_000},
            {"id": "tts-1-hd", "created": 1_800_000_000},
            {"id": "dall-e-3", "created": 1_800_000_000},
            {"id": "omni-moderation-latest", "created": 1_800_000_000},
            {"id": "gpt-5.4-mini", "created": 1_790_000_000},
            {"id": "gpt-4o-realtime-preview", "created": 1_800_000_000},
            {"id": "computer-use-preview", "created": 1_800_000_000},
            {"id": "davinci-002", "created": 1_800_000_000},
        ]
    }
    assert [m["id"] for m in parse_models("openai", body)] == [
        "gpt-5.4-mini",
        "gpt-4o-mini",
    ]


def test_groq_leaves_out_speech_and_guard_models_and_inactive_ones():
    body = {
        "data": [
            {"id": "llama-3.3-70b-versatile", "created": 1, "active": True},
            {"id": "whisper-large-v3", "created": 2, "active": True},
            {"id": "playai-tts", "created": 2, "active": True},
            {"id": "llama-guard-4-12b", "created": 2, "active": True},
            {"id": "mixtral-8x7b-32768", "created": 3, "active": False},
        ]
    }
    assert [m["id"] for m in parse_models("groq", body)] == ["llama-3.3-70b-versatile"]


def test_grok_takes_the_language_models_that_write_text():
    body = {
        "models": [
            {"id": "grok-4.3", "output_modalities": ["text"]},
            {"id": "grok-imagine-image", "output_modalities": ["image"]},
            {"id": "grok-4.7"},
        ]
    }
    assert [m["id"] for m in parse_models("grok", body)] == ["grok-4.3", "grok-4.7"]


def test_gemini_takes_the_models_that_generate_content_without_the_prefix():
    body = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "displayName": "Gemini 2.5 Flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/text-embedding-004",
                "displayName": "Embedding",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    assert parse_models("gemini", body) == [
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}
    ]


def test_cloudflare_takes_text_generation_models():
    body = {
        "result": [
            {
                "name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "task": {"name": "Text Generation"},
            },
            {"name": "@cf/baai/bge-base-en-v1.5", "task": {"name": "Text Embeddings"}},
        ]
    }
    assert [m["id"] for m in parse_models("cloudflare", body)] == [
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    ]


def test_a_reply_that_is_not_a_list_is_no_models():
    assert parse_models("openai", "nope") == []
    assert parse_models("anthropic", {"data": "x"}) == []


# ── the fetch ──────────────────────────────────────────────────────────────


def test_list_models_uses_the_hardened_client_and_hides_the_key_in_errors():
    body = {"data": [{"id": "gpt-5.4-mini", "created": 1}]}
    client = _client(_resp(body))
    assert list_models("openai", "sk-test-9", client=client) == [
        {"id": "gpt-5.4-mini", "label": "gpt-5.4-mini"}
    ]
    method, url = client.request.call_args.args
    assert method == "GET" and url == "https://api.openai.com/v1/models"
    assert (
        client.request.call_args.kwargs["headers"]["Authorization"]
        == "Bearer sk-test-9"
    )

    bad = _client(_resp({"error": {"message": "Incorrect API key: sk-test-9"}}, 401))
    with pytest.raises(AIProviderError) as e:
        list_models("openai", "sk-test-9", client=bad)
    assert "HTTP 401" in str(e.value) and "sk-test-9" not in str(e.value)

    with pytest.raises(AIProviderError, match="no text models"):
        list_models("openai", "sk-test-9", client=_client(_resp({"data": []})))


# ── the route ──────────────────────────────────────────────────────────────


@pytest.fixture
def anthropic_lists(monkeypatch):
    """Anthropic's list, as the hardened client would return it."""
    body = {
        "data": [
            {
                "id": "claude-sonnet-5-5",
                "display_name": "Claude Sonnet 5.5",
                "lifecycle": "active",
            },
            {
                "id": "claude-haiku-5-5",
                "display_name": "Claude Haiku 5.5",
                "lifecycle": "active",
            },
        ]
    }
    calls = []

    class _Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            c = _client(_resp(body))
            calls.append(c)
            return c

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ai_service, "_hardened_client", _Client)
    return calls


def test_the_route_lists_the_configured_providers_models(client, anthropic_lists):
    client.put(
        "/api/analytics/ai-config",
        json={"provider": "anthropic", "model": "", "api_key": ANT_KEY},
    )
    r = client.get("/api/analytics/ai-models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["provider"] == "anthropic" and body["provider_label"] == "Anthropic Claude"
    )
    assert [m["id"] for m in body["models"]] == [
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    assert ANT_KEY not in r.text
    sent = anthropic_lists[0].request.call_args.kwargs["headers"]
    assert sent["x-api-key"] == ANT_KEY


def test_the_route_needs_a_key_for_this_provider(client, anthropic_lists):
    client.put(
        "/api/analytics/ai-config",
        json={"provider": "anthropic", "model": "", "api_key": ANT_KEY},
    )
    client.put("/api/analytics/ai-config", json={"provider": "openai", "model": ""})
    r = client.get("/api/analytics/ai-models")
    assert r.status_code == 400 and "No API key saved for OpenAI" in r.json()["detail"]
    assert anthropic_lists == []  # nothing was asked


def test_the_route_reports_a_provider_that_cannot_list(client, monkeypatch):
    class _Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return _client(_resp({"error": "nope"}, 404))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ai_service, "_hardened_client", _Client)
    client.put(
        "/api/analytics/ai-config",
        json={
            "provider": "custom",
            "model": "",
            "endpoint_url": "https://api.example.com/v1",
            "api_key": "sk-gw-1",
        },
    )
    r = client.get("/api/analytics/ai-models")
    assert r.status_code == 502 and "HTTP 404" in r.json()["detail"]


def test_every_ai_call_needs_a_chosen_model(client, monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("a provider was called")

    monkeypatch.setattr(ai_service, "_hardened_client", refuse)
    client.put(
        "/api/analytics/ai-config",
        json={"provider": "openai", "model": "", "api_key": "sk-x"},
    )
    for r in (
        client.post("/api/analytics/ai-config/test", json={}),
        client.post("/api/analytics/ai-insights"),
        client.post("/api/analytics/ai-actions/top_customers"),
        client.post("/api/analytics/ai-query", params={"question": "Who owes most?"}),
    ):
        assert r.status_code == 400, r.text
        assert "No model chosen for OpenAI" in r.json()["detail"]


# ── the page ───────────────────────────────────────────────────────────────


def test_the_settings_page_lists_models_from_the_provider():
    src = open("app/static/js/settings.js", encoding="utf-8").read()
    assert "model_choices" not in src and "default_model" not in src
    # the page asks with what it holds — the provider picked and a key typed
    # but not yet saved (gate round 1, NEW-56 / W-4) — and nothing is stored
    assert (
        "API.post('/analytics/ai-models', SettingsPage._modelsRequest(provider))" in src
    )
    assert "API.get('/analytics/ai-models')" not in src
    # Save keeps a first key with no model chosen, lists, and asks for one
    assert (
        "toast('Key saved. Choose a model from the list, then Save.', 'success');"
        in src
    )
    assert 'id="ai-settings-models-refresh"' in src
    assert "Choose a model…" in src and "Enter a key to list models" in src
    # the picker fills once the saved key is this provider's, and again on a provider change
    assert (
        "SettingsPage._aiKeyOwner(cfg) === providerSel.value && !SettingsPage._aiModels[providerSel.value]"
        in src
    )
    assert (
        "if (SettingsPage._aiKeyOwner(cfg) === providerSel.value) loadModels();" in src
    )
    # Save and Test ask for a model
    assert "if (needsModel && !typedKey) { modelChosen(); return; }" in src
    assert "Key saved. Choose a model, then Test." in src


# ── the list for what the page holds (round 2: NEW-56 / W-4) ───────────────


def test_a_key_typed_and_not_yet_saved_lists_and_is_not_stored(client, monkeypatch):
    sent = {}

    def fake_list(provider, api_key, **kw):
        sent.update(provider=provider, api_key=api_key, **kw)
        return [{"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"}]

    monkeypatch.setattr(ai_service, "list_models", fake_list)
    r = client.post(
        "/api/analytics/ai-models", json={"provider": "anthropic", "api_key": ANT_KEY}
    )
    assert r.status_code == 200, r.text
    assert r.json()["models"][0]["id"] == "claude-sonnet-5-5"
    assert sent["provider"] == "anthropic" and sent["api_key"] == ANT_KEY
    cfg = client.get("/api/analytics/ai-config").json()
    assert cfg["has_api_key"] is False  # the list stored nothing


def test_the_typed_key_wins_over_the_saved_one_and_a_saved_key_counts_only_for_its_provider(
    client, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        ai_service, "list_models", lambda p, k, **kw: sent.append((p, k)) or []
    )
    r = client.put(
        "/api/analytics/ai-config",
        json={"provider": "anthropic", "model": "", "api_key": ANT_KEY},
    )
    assert r.status_code == 200, r.text
    # nothing typed: the saved key, for its own provider
    assert (
        client.post(
            "/api/analytics/ai-models", json={"provider": "anthropic"}
        ).status_code
        == 200
    )
    # typed: the typed one
    assert (
        client.post(
            "/api/analytics/ai-models",
            json={"provider": "anthropic", "api_key": "sk-ant-typed-1111"},
        ).status_code
        == 200
    )
    # another provider is not sent Anthropic's key
    r = client.post("/api/analytics/ai-models", json={"provider": "openai"})
    assert (
        r.status_code == 400
        and "No API key saved for OpenAI. Enter its key, then Refresh models" in r.text
    )
    assert sent == [("anthropic", ANT_KEY), ("anthropic", "sk-ant-typed-1111")]


def test_a_first_key_saves_with_no_model_and_the_analyses_still_ask_for_one(client):
    r = client.put(
        "/api/analytics/ai-config",
        json={"provider": "anthropic", "model": "", "api_key": ANT_KEY},
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_api_key"] is True and (r.json().get("model") or "") == ""
    r = client.post("/api/analytics/ai-config/test", json={})
    assert r.status_code == 400 and "No model chosen for Anthropic Claude" in r.text


def test_a_switch_away_and_back_keeps_the_saved_model_and_save_never_blanks_it():
    """Gate round 2, NEW-63: the provider change emptied the picker, and the
    next Save (or Test) wrote an empty model over the saved one with a green
    "Key saved…" though no key was typed."""
    src = open("app/static/js/settings.js", encoding="utf-8").read()
    assert (
        "const back = providerSel.value === cfg.provider ? (cfg.model || '') : '';"
        in src
    )
    assert (
        "modelCustom.value = back && SettingsPage._isCustomModel(known, back) ? back : '';"
        in src
    )
    # the key-only branch is for a typed key; otherwise a model is what is missing
    assert "if (needsModel && !typedKey) { modelChosen(); return; }" in src
    assert "if (needsModel && !keyPayload().api_key) { modelChosen(); return; }" in src
    # an empty list's text is the provider's own, not the page's
    assert "_modelsEmptyText: {}," in src
    assert "SettingsPage._modelsEmptyText[provider] = res.models.length" in src
    # macOS's auto-capitalisation stays out of the model and account boxes (NEW-68)
    assert (
        'id="ai-settings-model-custom" data-admin aria-label="Model ID" spellcheck="false" autocapitalize="off" autocorrect="off"'
        in src
    )
    assert (
        'id="ai-settings-cf-account" data-admin spellcheck="false" autocapitalize="off" autocorrect="off"'
        in src
    )
