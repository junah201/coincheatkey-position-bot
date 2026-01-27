import os

from dotenv import load_dotenv

load_dotenv()


def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        raise EnvironmentError(f"Required environment variable '{key}' not found.")
    return value
