from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    env: str = "development"
    log_level: str = "INFO"

    # Fyers — required from Step 4 onward
    fyers_client_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = "https://trade.fyers.in/api-login/redirect-uri/index.html"


settings = Settings()
