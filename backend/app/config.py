from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    KEYSTONE_API_KEY: str
    KEYSTONE_OWNER_ID: str
    OLLAMA_URL: str = "http://localhost:11434"
    EMBEDDING_MODEL: str = "nomic-embed-text"


settings = Settings()
