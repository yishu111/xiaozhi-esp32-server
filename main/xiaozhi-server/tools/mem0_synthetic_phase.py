#!/usr/bin/env python3
"""Insert same-request phase timings into an unmodified mem0 2.2.1 main.py.

The file must already hash to the upstream blob. This tool does not import
mem0, does not open Postgres, and does not edit server/main.py. A search is
timed only when every present user_id, agent_id, and run_id equals
diag-synthetic-not-a-user. The numbers are returned on that response as
synthetic_phase_ms and also emitted with logging.warning. Other searches
do not log and do not gain that key. Restart after install or uninstall.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

EXPECTED_SHA256 = "f329ca4a17af1fbefb32ac10d4cc7ae7ddbdb7435a87eb74c30915cff98b8b7c"
PATCHED_SHA256 = "dbde7297bb9a9d44967bfda27482abba4f5f779c38a69350b68a3e38e0ca309d"
SYNTHETIC_ENTITY = "diag-synthetic-not-a-user"
BACKUP_SUFFIX = ".mem0-synthetic-phase.bak"

_HELPER_OLD = """setup_config()
logger = logging.getLogger(__name__)

_UNSET = object()
"""

_HELPER_NEW = """setup_config()
logger = logging.getLogger(__name__)

_MEM0_SYNTHETIC_ENTITY = "diag-synthetic-not-a-user"


def _mem0_synthetic_filters(filters):
    if not isinstance(filters, dict):
        return False
    found = False
    for key in ("user_id", "agent_id", "run_id"):
        value = filters.get(key)
        if value in (None, ""):
            continue
        if value != _MEM0_SYNTHETIC_ENTITY:
            return False
        found = True
    return found


_UNSET = object()
"""

_STEPS_OLD = """        # Step 1: Preprocess query
        query_lemmatized = lemmatize_for_bm25(query)
        query_entities = extract_entities(query)

        # Step 2: Embed query
        embeddings = self.embedding_model.embed(query, "search")

        # Step 3: Semantic search (over-fetch for scoring pool)
        internal_limit = max(limit * 4, 60)
        semantic_results = self.vector_store.search(
            query=query, vectors=embeddings, top_k=internal_limit, filters=filters
        )

        # Step 4: Keyword search (if store supports it)
        keyword_results = self.vector_store.keyword_search(
            query=query_lemmatized, top_k=internal_limit, filters=filters
        )
"""

_STEPS_NEW = """        _mem0_phase = _mem0_synthetic_filters(filters)
        _mem0_origin = time.perf_counter() if _mem0_phase else 0.0

        # Step 1: Preprocess query
        query_lemmatized = lemmatize_for_bm25(query)
        query_entities = extract_entities(query)
        _mem0_pre = time.perf_counter() if _mem0_phase else 0.0

        # Step 2: Embed query
        embeddings = self.embedding_model.embed(query, "search")
        _mem0_embed = time.perf_counter() if _mem0_phase else 0.0

        # Step 3: Semantic search (over-fetch for scoring pool)
        internal_limit = max(limit * 4, 60)
        semantic_results = self.vector_store.search(
            query=query, vectors=embeddings, top_k=internal_limit, filters=filters
        )
        _mem0_vector = time.perf_counter() if _mem0_phase else 0.0

        # Step 4: Keyword search (if store supports it)
        keyword_results = self.vector_store.keyword_search(
            query=query_lemmatized, top_k=internal_limit, filters=filters
        )
        _mem0_keyword = time.perf_counter() if _mem0_phase else 0.0
"""

_ENTITY_OLD = """        if query_entities:
            entity_boosts = self._compute_entity_boosts(query_entities, filters)

        # Step 7: Build candidate set from semantic results
"""

_ENTITY_NEW = """        if query_entities:
            entity_boosts = self._compute_entity_boosts(query_entities, filters)
        _mem0_entity = time.perf_counter() if _mem0_phase else 0.0

        # Step 7: Build candidate set from semantic results
"""

_RETURN_OLD = """            original_memories.append(memory_item_dict)

        return original_memories

    def _compute_entity_boosts(self, query_entities, filters):
"""

_RETURN_NEW = """            original_memories.append(memory_item_dict)

        _mem0_phase_report = None
        if _mem0_phase:
            _mem0_done = time.perf_counter()
            _mem0_phase_report = {
                "preprocess_ms": round((_mem0_pre - _mem0_origin) * 1000.0, 1),
                "embed_ms": round((_mem0_embed - _mem0_pre) * 1000.0, 1),
                "vector_ms": round((_mem0_vector - _mem0_embed) * 1000.0, 1),
                "keyword_ms": round((_mem0_keyword - _mem0_vector) * 1000.0, 1),
                "entity_ms": round((_mem0_entity - _mem0_keyword) * 1000.0, 1),
                "rank_ms": round((_mem0_done - _mem0_entity) * 1000.0, 1),
                "total_ms": round((_mem0_done - _mem0_origin) * 1000.0, 1),
                "entity_ran": 1 if query_entities else 0,
                "semantic_n": len(semantic_results) if semantic_results is not None else -1,
                "keyword_n": len(keyword_results) if keyword_results is not None else -1,
                "returned_n": len(original_memories),
            }
            try:
                logging.warning(
                    "mem0_synthetic_phase preprocess_ms=%.1f embed_ms=%.1f vector_ms=%.1f keyword_ms=%.1f entity_ms=%.1f rank_ms=%.1f total_ms=%.1f entity_ran=%d semantic_n=%d keyword_n=%d returned_n=%d",
                    _mem0_phase_report["preprocess_ms"],
                    _mem0_phase_report["embed_ms"],
                    _mem0_phase_report["vector_ms"],
                    _mem0_phase_report["keyword_ms"],
                    _mem0_phase_report["entity_ms"],
                    _mem0_phase_report["rank_ms"],
                    _mem0_phase_report["total_ms"],
                    _mem0_phase_report["entity_ran"],
                    _mem0_phase_report["semantic_n"],
                    _mem0_phase_report["keyword_n"],
                    _mem0_phase_report["returned_n"],
                )
            except Exception:
                pass
        return original_memories, _mem0_phase_report

    def _compute_entity_boosts(self, query_entities, filters):
"""

_CALL_OLD = """        search_start = time.perf_counter()
        original_memories = self._search_vector_store(
            query, effective_filters, limit, threshold, explain=explain, show_expired=show_expired
        )
        search_elapsed_seconds = time.perf_counter() - search_start
"""

_CALL_NEW = """        search_start = time.perf_counter()
        original_memories, _mem0_phase_report = self._search_vector_store(
            query, effective_filters, limit, threshold, explain=explain, show_expired=show_expired
        )
        search_elapsed_seconds = time.perf_counter() - search_start
"""

_RESPONSE_OLD = """            display_first_run_notice(self, "sync", "search")
        return {"results": original_memories}
"""

_RESPONSE_NEW = """            display_first_run_notice(self, "sync", "search")
        response = {"results": original_memories}
        if _mem0_phase_report:
            response["synthetic_phase_ms"] = _mem0_phase_report
        return response
"""

_REPLACEMENTS = (
    (_HELPER_OLD, _HELPER_NEW),
    (_STEPS_OLD, _STEPS_NEW),
    (_ENTITY_OLD, _ENTITY_NEW),
    (_RETURN_OLD, _RETURN_NEW),
    (_CALL_OLD, _CALL_NEW),
    (_RESPONSE_OLD, _RESPONSE_NEW),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def apply_patch(text: str) -> str:
    for old, new in _REPLACEMENTS:
        count = text.count(old)
        if count != 1:
            raise ValueError(f"anchor count {count}")
        text = text.replace(old, new, 1)
    return text


def synthetic_filters(filters) -> bool:
    if not isinstance(filters, dict):
        return False
    found = False
    for key in ("user_id", "agent_id", "run_id"):
        value = filters.get(key)
        if value in (None, ""):
            continue
        if value != SYNTHETIC_ENTITY:
            return False
        found = True
    return found


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def install(path: Path) -> str:
    data = path.read_bytes()
    digest = sha256_bytes(data)
    if digest == PATCHED_SHA256:
        return "already_installed"
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"refusing_hash {digest}")
    backup = _backup_path(path)
    if backup.exists() and sha256_file(backup) != EXPECTED_SHA256:
        raise SystemExit("refusing_backup")
    patched = apply_patch(data.decode("utf-8")).encode("utf-8")
    patched_digest = sha256_bytes(patched)
    if patched_digest != PATCHED_SHA256:
        raise SystemExit(f"refusing_patched_hash {patched_digest}")
    if not backup.exists():
        backup.write_bytes(data)
    path.write_bytes(patched)
    if sha256_file(path) != PATCHED_SHA256:
        path.write_bytes(data)
        raise SystemExit("refusing_written_hash")
    return "installed"


def uninstall(path: Path) -> str:
    backup = _backup_path(path)
    if not backup.exists():
        raise SystemExit("missing_backup")
    if sha256_file(backup) != EXPECTED_SHA256:
        raise SystemExit("refusing_backup")
    current = sha256_file(path)
    if current not in {PATCHED_SHA256, EXPECTED_SHA256}:
        raise SystemExit(f"refusing_hash {current}")
    shutil.copyfile(backup, path)
    if sha256_file(path) != EXPECTED_SHA256:
        raise SystemExit("restore_mismatch")
    return "restored"


def check(path: Path) -> str:
    digest = sha256_file(path)
    if digest == EXPECTED_SHA256:
        return "original"
    if digest == PATCHED_SHA256:
        return "patched"
    raise SystemExit(f"refusing_hash {digest}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install mem0 synthetic phase timings")
    parser.add_argument("action", choices=("install", "uninstall", "check"))
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "install":
        result = install(args.path)
    elif args.action == "uninstall":
        result = uninstall(args.path)
    else:
        result = check(args.path)
    sys.stdout.write(result + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
