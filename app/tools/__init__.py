"""Herramientas funcionales de los agentes (inspección, reglas y validación estructural)."""
from app.tools.file_inspector import inspect_localization_files
from app.tools.l10n_rules import search_l10n_qa_rules
from app.tools.structural_validator import (
    check_placeholders_and_newlines,
    compare_json_structure,
    validate_rules_and_exceptions,
)

__all__ = [
    "inspect_localization_files",
    "search_l10n_qa_rules",
    "compare_json_structure",
    "check_placeholders_and_newlines",
    "validate_rules_and_exceptions",
]
