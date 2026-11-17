"""Keep 64-bit X Snowflake identifiers lossless in JavaScript clients."""
from fastapi.responses import JSONResponse


def safe_numbers(value):
    if isinstance(value, dict):
        return {key: safe_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_numbers(item) for item in value]
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(safe_numbers(content))
