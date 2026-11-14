from cryptography.fernet import Fernet

from .config import get_settings


def _f() -> Fernet:
    return Fernet(get_settings().fernet_key())


def encrypt(value: str) -> str:
    return _f().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return _f().decrypt(value.encode()).decode() if value else ""
