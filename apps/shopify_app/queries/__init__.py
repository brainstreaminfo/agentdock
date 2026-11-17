"""`.graphql` documents live in this folder. Load one with `load_query("shop_info")`."""

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def load_query(name: str) -> str:
    return (_DIR / f"{name}.graphql").read_text()
