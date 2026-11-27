import re
from typing import Annotated

from pydantic import Field

MAX_DB_ID = 2**63 - 1
DatabaseId = Annotated[int, Field(gt=0, le=MAX_DB_ID)]


def parse_id(value: str) -> int | None:
    if not re.fullmatch(r"[0-9]{1,19}", value.strip()):
        return None
    number = int(value)
    return number if 0 < number <= MAX_DB_ID else None
