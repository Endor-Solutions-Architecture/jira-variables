"""Errors returned to Endor as Jira-shaped HTTP responses."""


class ShimError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def jira_error_body(message: str) -> bytes:
    import json

    return json.dumps({"errorMessages": [message], "errors": {}}).encode()
