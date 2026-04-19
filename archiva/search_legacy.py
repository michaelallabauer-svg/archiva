"""Compatibility wrapper for legacy Postgres search helpers."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_legacy_path = Path(__file__).with_name("search.py")
_spec = spec_from_file_location("archiva._legacy_search_module", _legacy_path)
if _spec is None or _spec.loader is None:
    raise ImportError("Legacy search module could not be loaded")
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)

build_auto_complete_query = _module.build_auto_complete_query
build_search_query = _module.build_search_query
update_document_vector = _module.update_document_vector

__all__ = ["build_auto_complete_query", "build_search_query", "update_document_vector"]
