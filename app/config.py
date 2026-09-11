"""Application settings, loaded from environment variables / `.env`."""

import os
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class SqlDatabase(BaseModel):
    """An external SQL database the agent is allowed to query."""

    url: str
    description: str = ""
    read_only: bool = False
    notes: str = ""  # business definitions for text-to-SQL, e.g. "revenue = SUM(orders.amount)"


class RagSqlTable(BaseModel):
    """A SQL table whose rows are embedded into the knowledge base."""

    database: str
    table: str
    text_columns: list[str]
    id_column: str = "id"
    title_column: str | None = None
    date_column: str | None = None
    url_column: str | None = None


CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    # ENV_FILE selects another settings file, e.g. ENV_FILE=demo/.env.demo; tests point it at nothing.
    model_config = SettingsConfigDict(env_file=os.getenv("ENV_FILE", ".env"), env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    app_database_url: str = "sqlite+aiosqlite:///data/app.db"

    # --- Gmail inbox & Pub/Sub push ----------------------------------------
    agent_email: str = ""
    allowed_senders: CommaList = []
    gmail_pubsub_topic: str = ""
    pubsub_audience: str = ""
    pubsub_service_account: str = ""
    verify_pubsub_jwt: bool = True

    # --- Google OAuth ------------------------------------------------------
    google_client_secrets_file: Path = Path("credentials.json")
    google_token_file: Path = Path("data/token.json")

    # --- LLM providers -----------------------------------------------------
    default_model: str = "gemini-2.5-flash"
    router_model: str = ""  # a cheaper model for routing, e.g. gemini-3.1-flash-lite; default_model if empty
    llm_timeout_s: float = 120.0  # hard cap for one LLM call or tool loop
    planning_thinking_budget: int = 1024  # reasoning-token cap for routing / SQL planning (Gemini 2.5)
    gemini_api_keys: CommaList = []
    gemini_fallback_api_key: str = ""
    xai_api_key: str = ""
    router_confidence_threshold: float = 0.6

    # --- Specialists -------------------------------------------------------
    sql_databases: dict[str, SqlDatabase] = {}
    sql_max_rows: int = 200
    sql_sample_values: bool = True  # show distinct values of low-cardinality text columns in the schema
    finance_enabled: bool = True
    alpha_vantage_api_key: str = ""

    # --- Knowledge base (RAG) ----------------------------------------------
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "knowledge_base"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimension: int = 768
    rag_chunk_size: int = 1000
    rag_chunk_overlap: int = 200
    rag_top_k: int = 8
    rag_score_threshold: float = 0.3
    rag_title_in_chunks: bool = True
    rag_sql_tables: list[RagSqlTable] = []

    # --- Google Drive sync -------------------------------------------------
    drive_folder_ids: CommaList = []
    drive_webhook_url: str = ""
    drive_channel_token: str = ""

    # --- Workflows ---------------------------------------------------------
    workflow_sheet_id: str = ""
    workflow_sheet_tab: str = "Sheet1"
    workflow_cache_ttl: int = 60

    # --- Permissions -------------------------------------------------------
    permissions_enabled: bool = True
    default_user_role: str = "guest"
    admin_email: str = ""
    permissions_cache_ttl: int = 300

    @field_validator("allowed_senders", "gemini_api_keys", "drive_folder_ids", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("sql_databases", mode="before")
    @classmethod
    def _expand_database_urls(cls, value: Any) -> Any:
        # Allow the short form {"crm": "mysql+aiomysql://..."} next to the full object form.
        if isinstance(value, dict):
            return {name: {"url": cfg} if isinstance(cfg, str) else cfg for name, cfg in value.items()}
        return value

    @property
    def workflows_enabled(self) -> bool:
        return bool(self.workflow_sheet_id)

    @property
    def drive_sync_enabled(self) -> bool:
        return bool(self.drive_folder_ids and self.drive_webhook_url)


settings = Settings()
