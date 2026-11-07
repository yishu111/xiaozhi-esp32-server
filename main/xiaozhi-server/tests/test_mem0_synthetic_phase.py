"""The phase insert matches one mem0 blob and does not time real identities."""

import ast
import json
import logging
import textwrap
import typing
from pathlib import Path

import pytest

from tools.mem0_synthetic_phase import (
    EXPECTED_SHA256,
    PATCHED_SHA256,
    SYNTHETIC_ENTITY,
    _CALL_OLD,
    _ENTITY_OLD,
    _HELPER_OLD,
    _RESPONSE_OLD,
    _RETURN_OLD,
    _STEPS_OLD,
    apply_patch,
    install,
    sha256_file,
    synthetic_filters,
    uninstall,
)

QUERY_SENTINEL = "QUERY_TEXT_SHOULD_NOT_LEAK"
MEMORY_SENTINEL = "MEMORY_TEXT_SHOULD_NOT_LEAK"


def test_only_the_marker_identity_is_timed():
    assert synthetic_filters({"user_id": SYNTHETIC_ENTITY})
    assert synthetic_filters(
        {"user_id": SYNTHETIC_ENTITY, "agent_id": SYNTHETIC_ENTITY, "run_id": ""}
    )
    assert not synthetic_filters({"user_id": "t123u"})
    assert not synthetic_filters({"user_id": SYNTHETIC_ENTITY, "agent_id": "t123u"})
    assert not synthetic_filters({})
    assert not synthetic_filters(None)
    assert not synthetic_filters({"AND": [{"user_id": SYNTHETIC_ENTITY}]})


def test_patch_keeps_sync_call_order_and_the_async_line():
    decoy = (
        'embeddings = await asyncio.to_thread(self.embedding_model.embed, query, "search")\n'
        "original_memories = await self._search_vector_store(\n"
        '            display_first_run_notice_async(self, "async", "search")\n'
    )
    patched = apply_patch(
        _HELPER_OLD + decoy + _STEPS_OLD + _ENTITY_OLD + _RETURN_OLD + _CALL_OLD + _RESPONSE_OLD
    )
    assert patched.count("def _mem0_synthetic_filters") == 1
    assert "await self._search_vector_store" in patched
    assert "display_first_run_notice_async" in patched
    embed_at = patched.index('embeddings = self.embedding_model.embed(query, "search")')
    vector_at = patched.index("self.vector_store.search(")
    keyword_at = patched.index("self.vector_store.keyword_search(")
    entity_at = patched.index("self._compute_entity_boosts(")
    assert embed_at < vector_at < keyword_at < entity_at
    log_line = patched.split("mem0_synthetic_phase ", 1)[1].split("\n", 1)[0]
    assert "query" not in log_line
    assert "logging.warning(" in patched
    assert "logger.info(" not in patched
    for name in ("preprocess_ms", "embed_ms", "vector_ms", "keyword_ms", "entity_ms", "rank_ms", "total_ms"):
        assert name in log_line


def test_install_leaves_unexpected_bytes_untouched(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("not the library\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        install(target)
    assert "refusing_hash" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "not the library\n"


def _upstream_text():
    candidates = (
        Path("/tmp/mem0_main_221.py"),
        Path(__file__).resolve().parent / "fixtures" / "mem0_memory_main_2.2.1.py",
    )
    source = next((path for path in candidates if path.is_file() and sha256_file(path) == EXPECTED_SHA256), None)
    if source is None:
        pytest.skip("upstream mem0 2.2.1 main.py is not in this environment")
    return source.read_text(encoding="utf-8")


def test_roundtrip_on_exact_upstream_blob(tmp_path):
    source = _upstream_text()
    target = tmp_path / "main.py"
    target.write_text(source, encoding="utf-8")
    assert install(target) == "installed"
    assert sha256_file(target) == PATCHED_SHA256
    assert b"POSTGRES_PASSWORD" not in target.read_bytes()
    assert install(target) == "already_installed"
    assert uninstall(target) == "restored"
    assert sha256_file(target) == EXPECTED_SHA256


class _Hit:
    def __init__(self, payload):
        self.id = "m1"
        self.score = 0.9
        self.payload = payload


class _Embed:
    def embed(self, text, action):
        self.text = text
        self.action = action
        return [0.1, 0.2]


class _Store:
    def search(self, query, vectors, top_k, filters):
        self.query = query
        self.filters = filters
        return [_Hit({"data": MEMORY_SENTINEL, "user_id": filters.get("user_id")})]

    def keyword_search(self, query, top_k, filters):
        return None


class _MemoryItem:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def model_dump(self):
        return dict(self.kwargs)


def _route_filters(payload):
    """Same merge as the reviewed POST /search handler."""
    filters = dict(payload.get("filters") or {})
    for key in ("user_id", "agent_id", "run_id"):
        value = payload.get(key)
        if value:
            filters[key] = value
    return filters


def _load_search(patched):
    tree = ast.parse(patched)
    helper = None
    methods = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_mem0_synthetic_filters":
            helper = ast.get_source_segment(patched, node)
        if isinstance(node, ast.ClassDef) and node.name == "Memory":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in {"search", "_search_vector_store"}:
                    methods.append(textwrap.indent(ast.get_source_segment(patched, item), "    "))
    source = helper + "\n\nclass Memory:\n" + "\n".join(methods) + "\n"
    namespace = {
        "Optional": typing.Optional,
        "Dict": typing.Dict,
        "Any": typing.Any,
        "time": __import__("time"),
        "logging": logging,
        "logger": logging.getLogger("mem0.memory.main"),
        "MemoryItem": _MemoryItem,
        "PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS": 10**9,
        "get_temporal_feature_error_message": lambda *args: "temporal",
        "_reject_top_level_entity_params": lambda *args: None,
        "_validate_search_params": lambda **kwargs: None,
        "_validate_and_trim_search_query": lambda query: query,
        "detect_temporal_usage_from_search": lambda *args: None,
        "_validate_and_trim_entity_id": lambda value, name: None if value is None else str(value).strip(),
        "detect_scale_threshold_from_top_k": lambda top_k: None,
        "process_telemetry_filters": lambda filters: ([], []),
        "capture_event": lambda *args, **kwargs: None,
        "display_temporal_usage_notice": lambda *args: None,
        "display_scale_threshold_notice": lambda *args: None,
        "display_performance_slow_query_notice": lambda *args: None,
        "display_first_run_notice": lambda *args: None,
        "lemmatize_for_bm25": lambda query: query,
        "extract_entities": lambda query: [],
        "get_bm25_params": lambda *args, **kwargs: (1.0, 1.0),
        "normalize_bm25": lambda *args: 0.0,
        "score_and_rank": lambda semantic_results, **kwargs: semantic_results,
        "_payload_is_expired": lambda payload: False,
        "_MEM0_SYNTHETIC_ENTITY": SYNTHETIC_ENTITY,
    }
    exec(source, namespace)
    memory = namespace["Memory"]()
    memory.api_version = "v1.1"
    memory.reranker = None
    memory.embedding_model = _Embed()
    memory.vector_store = _Store()
    memory._has_advanced_operators = lambda filters: False
    memory._compute_entity_boosts = lambda *args: {}
    return memory


def _capture(level):
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(level)
    root.addHandler(handler)
    return records, handler, previous


def _release(handler, previous):
    root = logging.getLogger()
    root.removeHandler(handler)
    root.setLevel(previous)


def test_synthetic_route_returns_one_phase_without_text():
    memory = _load_search(apply_patch(_upstream_text()))
    payload = {"query": QUERY_SENTINEL, "user_id": SYNTHETIC_ENTITY, "top_k": 8}
    records, handler, previous = _capture(logging.WARNING)
    try:
        response = memory.search(payload["query"], filters=_route_filters(payload), top_k=8)
    finally:
        _release(handler, previous)
    markers = [record for record in records if record.getMessage().startswith("mem0_synthetic_phase ")]
    assert len(markers) == 1
    assert markers[0].levelno == logging.WARNING
    phase = response["synthetic_phase_ms"]
    rendered = markers[0].getMessage() + json.dumps(phase, sort_keys=True)
    assert QUERY_SENTINEL not in rendered
    assert MEMORY_SENTINEL not in rendered
    assert response["results"][0]["memory"] == MEMORY_SENTINEL
    for name in ("preprocess_ms", "embed_ms", "vector_ms", "keyword_ms", "entity_ms", "rank_ms", "total_ms"):
        assert isinstance(phase[name], float)
    assert phase["entity_ran"] == 0
    assert phase["returned_n"] == 1


def test_filters_body_matches_and_real_identity_is_silent():
    memory = _load_search(apply_patch(_upstream_text()))
    records, handler, previous = _capture(logging.INFO)
    try:
        via_filters = memory.search(
            QUERY_SENTINEL,
            filters=_route_filters({"query": QUERY_SENTINEL, "filters": {"user_id": SYNTHETIC_ENTITY}}),
        )
        real = memory.search("hello", filters=_route_filters({"user_id": "t123u"}))
        mixed = memory.search(
            QUERY_SENTINEL,
            filters=_route_filters({"user_id": SYNTHETIC_ENTITY, "agent_id": "t123u"}),
        )
    finally:
        _release(handler, previous)
    assert "synthetic_phase_ms" in via_filters
    assert "synthetic_phase_ms" not in real
    assert "synthetic_phase_ms" not in mixed
    leaked = [
        record.getMessage()
        for record in records
        if record.getMessage().startswith("mem0_synthetic_phase ") and "t123u" in record.getMessage()
    ]
    assert leaked == []
    phase_messages = [record for record in records if record.getMessage().startswith("mem0_synthetic_phase ")]
    assert len(phase_messages) == 1


def test_warning_survives_when_info_is_filtered():
    memory = _load_search(apply_patch(_upstream_text()))
    records, handler, previous = _capture(logging.WARNING)
    library_logger = logging.getLogger("mem0.memory.main")
    library_level = library_logger.level
    library_logger.setLevel(logging.ERROR)
    try:
        response = memory.search(QUERY_SENTINEL, filters={"user_id": SYNTHETIC_ENTITY})
    finally:
        library_logger.setLevel(library_level)
        _release(handler, previous)
    assert response["synthetic_phase_ms"]["returned_n"] == 1
    assert any(
        record.levelno == logging.WARNING and record.getMessage().startswith("mem0_synthetic_phase ")
        for record in records
    )
