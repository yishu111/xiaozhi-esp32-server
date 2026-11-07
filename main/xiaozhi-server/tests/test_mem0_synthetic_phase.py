"""The phase insert matches one mem0 blob and does not time real identities."""

from pathlib import Path

import pytest

from tools.mem0_synthetic_phase import (
    EXPECTED_SHA256,
    PATCHED_SHA256,
    SYNTHETIC_ENTITY,
    _ENTITY_OLD,
    _HELPER_OLD,
    _RETURN_OLD,
    _STEPS_OLD,
    apply_patch,
    install,
    sha256_file,
    synthetic_filters,
    uninstall,
)


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
    decoy = 'embeddings = await asyncio.to_thread(self.embedding_model.embed, query, "search")\n'
    patched = apply_patch(_HELPER_OLD + decoy + _STEPS_OLD + _ENTITY_OLD + _RETURN_OLD)
    assert patched.count("def _mem0_synthetic_filters") == 1
    assert decoy in patched
    embed_at = patched.index('embeddings = self.embedding_model.embed(query, "search")')
    vector_at = patched.index("self.vector_store.search(")
    keyword_at = patched.index("self.vector_store.keyword_search(")
    entity_at = patched.index("self._compute_entity_boosts(")
    assert embed_at < vector_at < keyword_at < entity_at
    log_line = patched.split("mem0_synthetic_phase ", 1)[1].split("\n", 1)[0]
    assert "query" not in log_line
    for name in ("preprocess_ms", "embed_ms", "vector_ms", "keyword_ms", "entity_ms", "rank_ms", "total_ms"):
        assert name in log_line


def test_install_leaves_unexpected_bytes_untouched(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("not the library\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        install(target)
    assert "refusing_hash" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "not the library\n"


def test_roundtrip_on_exact_upstream_blob(tmp_path):
    candidates = (
        Path("/tmp/mem0_main_221.py"),
        Path(__file__).resolve().parent / "fixtures" / "mem0_memory_main_2.2.1.py",
    )
    source = next((path for path in candidates if path.is_file() and sha256_file(path) == EXPECTED_SHA256), None)
    if source is None:
        pytest.skip("upstream mem0 2.2.1 main.py is not in this environment")
    target = tmp_path / "main.py"
    target.write_bytes(source.read_bytes())
    assert install(target) == "installed"
    assert sha256_file(target) == PATCHED_SHA256
    assert b"POSTGRES_PASSWORD" not in target.read_bytes()
    assert install(target) == "already_installed"
    assert uninstall(target) == "restored"
    assert sha256_file(target) == EXPECTED_SHA256
