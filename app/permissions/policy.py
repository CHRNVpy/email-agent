"""Permission vocabulary, default roles and the (pure) matching rules.

Permissions are colon-separated strings: `<resource>:<action>` or, for SQL,
`sql:<database>:<read|write>`. Grants may contain `*` wildcards, so
`sql:*:read` allows reading every configured database and `*` allows everything.
An explicit denial always wins over any grant.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

DEFAULT_PERMISSIONS: dict[str, str] = {
    "*": "Everything",
    "sql:*:read": "Read-only queries on any configured SQL database",
    "sql:*:write": "INSERT / UPDATE / DELETE on any configured SQL database",
    "knowledge:read": "Search the knowledge base (RAG)",
    "finance:read": "Quotes, fundamentals, earnings and market news",
    "sheets:read": "Read Google Sheets",
    "sheets:write": "Update and append to Google Sheets",
    "web:search": "Google Search grounding and reading URLs",
    "files:read": "Analyse email attachments",
    "workflows:run": "Trigger workflows defined in the workflow sheet",
}

DEFAULT_ROLES: dict[str, tuple[str, list[str]]] = {
    "admin": ("Full access", ["*"]),
    "analyst": (
        "Research, finance and reporting",
        [
            "finance:read",
            "web:search",
            "knowledge:read",
            "files:read",
            "sheets:read",
            "sheets:write",
            "sql:*:read",
            "workflows:run",
        ],
    ),
    "operator": (
        "Data operations",
        ["sql:*:read", "sql:*:write", "sheets:read", "sheets:write", "knowledge:read", "files:read", "workflows:run"],
    ),
    "guest": ("Limited access", ["web:search", "knowledge:read", "files:read"]),
}


def matches(required: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(required, pattern) for pattern in patterns)


@dataclass(frozen=True)
class Principal:
    """Who the agent is acting for, with pre-resolved effective permissions."""

    email: str
    user_id: int | None = None
    granted: frozenset[str] = field(default_factory=frozenset)
    denied: frozenset[str] = field(default_factory=frozenset)
    unrestricted: bool = False  # permissions disabled globally

    def can(self, permission: str) -> bool:
        if self.unrestricted:
            return True
        return not matches(permission, self.denied) and matches(permission, self.granted)
