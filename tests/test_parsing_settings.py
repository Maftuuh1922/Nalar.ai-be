"""Unit test logika murni service document_parsing & capability_settings.

Tidak butuh server hidup maupun database: hanya fungsi-fungsi yang tidak
mengakses session DB. Kontrak API penuh diuji terpisah lewat
``verify_changes.py`` (smoke test integrasi terhadap backend 8087).
"""

import uuid

from app.models.document_parsing import DocumentParsingSetting
from app.services.capability_settings import (
    DEFAULT_CAPABILITY_SETTINGS,
    _deep_merge,
)
from app.services.document_parsing import (
    DEFAULT_OPTIONS,
    ENGINE_META,
    ENGINE_IDS,
    INSTALL_PACKAGES,
    docling_models_ready,
    find_mineru_cli,
    is_installed,
    readiness_for,
)


def _row(**overrides) -> DocumentParsingSetting:
    values = {"user_id": uuid.uuid4(), "engine": "text_only", "options_json": "{}", "mineru_json": "{}"}
    values.update(overrides)
    return DocumentParsingSetting(**values)


class TestEngineMeta:
    def test_meta_covers_all_engine_ids(self):
        assert set(ENGINE_META) == set(ENGINE_IDS)

    def test_meta_has_required_fields(self):
        for meta in ENGINE_META.values():
            assert {"name", "description", "needs_local_models"} <= set(meta)

    def test_default_options_only_for_option_engines(self):
        assert set(DEFAULT_OPTIONS) <= set(ENGINE_IDS)

    def test_install_packages_valid_engines(self):
        assert set(INSTALL_PACKAGES) <= set(ENGINE_IDS)


class TestAvailability:
    def test_text_only_never_installable(self):
        assert "text_only" not in INSTALL_PACKAGES

    def test_markitdown_detected_as_installed(self):
        # Terpasang nyata lewat endpoint /install di sesi verifikasi.
        assert is_installed("markitdown") is True

    def test_docling_not_installed(self):
        assert is_installed("docling") is False

    def test_find_mineru_cli_empty_path(self):
        cli = find_mineru_cli("")
        assert cli["found"] is False
        assert cli["source"] == "path"

    def test_docling_models_ready_returns_bool(self):
        assert isinstance(docling_models_ready(), bool)


class TestReadiness:
    def test_text_only_always_ready(self):
        r = readiness_for("text_only", _row())
        assert r["ready"] is True

    def test_uninstalled_engine_not_ready(self):
        r = readiness_for("docling", _row())
        assert r["ready"] is False
        assert r["reason"] == "not_installed"

    def test_installed_engine_ready(self):
        r = readiness_for("markitdown", _row())
        assert r["ready"] is True

    def test_mineru_unconfigured_not_ready(self):
        r = readiness_for("mineru", _row(), {"api_token_set": False, "local_cli": {"found": False}})
        assert r["ready"] is False
        assert r["reason"] == "not_configured"

    def test_mineru_cloud_token_ready(self):
        r = readiness_for("mineru", _row(), {"api_token_set": True, "local_cli": {"found": False}})
        assert r["ready"] is True


class TestCapabilityDefaults:
    def test_all_seven_blocks(self):
        assert set(DEFAULT_CAPABILITY_SETTINGS) == {
            "chat", "solve", "research", "question",
            "co_writer", "vision_solver", "math_animator",
        }

    def test_chat_has_stage_budgets(self):
        chat = DEFAULT_CAPABILITY_SETTINGS["chat"]
        assert set(chat["stage_budgets"]) == {"exploring", "responding"}

    def test_every_block_has_temperature(self):
        for block in DEFAULT_CAPABILITY_SETTINGS.values():
            assert isinstance(block.get("temperature"), (int, float))

    def test_deep_merge_override_wins(self):
        base = {"chat": {"temperature": 0.7, "stage_budgets": {"exploring": 2, "responding": 1}}}
        override = {"chat": {"temperature": 0.3}}
        merged = _deep_merge(base, override)
        assert merged["chat"]["temperature"] == 0.3
        assert merged["chat"]["stage_budgets"]["responding"] == 1

    def test_deep_merge_nested_override(self):
        base = {"question": {"exploring": {"tool_summarizer": {"enabled": True, "max_tokens": 2000}}}}
        override = {"question": {"exploring": {"tool_summarizer": {"enabled": False}}}}
        merged = _deep_merge(base, override)
        assert merged["question"]["exploring"]["tool_summarizer"] == {"enabled": False, "max_tokens": 2000}
