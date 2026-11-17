"""App settings, read from environment variables (12-factor style).

Example: DATABASE_URL=postgresql+psycopg://user:pass@db:5432/shortener
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./dev.db"
    # Public base used to build short links, e.g. https://sho.rt
    base_url: str = "http://localhost:8000"
    # Length of randomly generated codes. 62^7 ~= 3.5 trillion possibilities.
    code_length: int = 7


settings = Settings()
