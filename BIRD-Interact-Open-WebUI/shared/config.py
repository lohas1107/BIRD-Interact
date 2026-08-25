"""Centralized configuration.

Settings are loaded in this priority (highest wins):
  1. Environment variables (e.g. PATIENCE=6 python -m orchestrator.runner ...)
  2. .env file in project root (user-specific, gitignored)
  3. Defaults defined below

Users: copy .env.example to .env and edit.
See .env.example for all available settings.
"""

from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env into os.environ for local development.
load_dotenv()


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Open WebUI backend API
    open_webui_base_url: str = "http://127.0.0.1:3000"
    open_webui_api_key: str = ""
    open_webui_timeout: float = 180.0
    open_webui_max_tokens: int = 4096

    # PostgreSQL
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "root"
    pg_password: str = "123123"
    pg_minconn: int = 1
    pg_maxconn: int = 5

    # Service ports
    system_agent_port: int = 6000
    user_sim_port: int = 6001
    db_env_port: int = 6002

    # Optional kg-v1 embedding gateway.  The gateway is only started when
    # KG_ENABLED=1; keeping these settings here lets the DB service fail
    # closed without changing the legacy startup path.
    embedding_service_port: int = 6003
    embedding_service_url: str = "http://127.0.0.1:6003"
    embedding_api_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_timeout: float = 60.0
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Optional kg-v1 Neo4j physical-schema and Knowledge graph.
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "bird-interact-dev"
    neo4j_database: str = "neo4j"

    # Models exposed by the configured Open WebUI provider
    system_agent_model: str = "google/gemma-4-31B-it"
    user_simulator_model: str = "google/gemma-4-31B-it"

    # Dataset: "lite" or "full"
    dataset: str = "lite"

    # User simulator prompt version: "v1" (legacy) or "v2" (recommended)
    prompt_version: str = "v2"

    # Budget / turns
    patience: int = 3

    @property
    def project_root(self) -> Path:
        """Project root used by configuration-backed runtime assets."""
        return PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / f"bird-interact-{self.dataset}"

    @property
    def data_path(self) -> str:
        return str(self.data_dir / "bird_interact_data.jsonl")

    @property
    def db_data_path(self) -> str:
        return str(self.data_dir)

    @property
    def metadata_manifest_path(self) -> Path:
        """Fixed metadata corpus manifest used by the metadata-5-2 profile."""
        return self.data_dir / "metadata_manifest.json"

    @property
    def metadata_cache_dir(self) -> Path:
        """Gitignored vector cache for metadata-5-2 retrieval."""
        return self.project_root / ".cache" / "metadata-5-2-kg"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
