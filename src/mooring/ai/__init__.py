"""AI assistance for analysts: schema-only code generation.

A provider turns a dataset *schema* (column names + dtypes — never any data
values) plus a plain-English goal into ready-to-paste code. GitHub Copilot is
the only backend today; ``base.AIProvider`` is the seam for future licences.
"""

from mooring.ai.base import AIError, AIProvider, ProviderStatus, get_provider

__all__ = ["AIError", "AIProvider", "ProviderStatus", "get_provider"]
