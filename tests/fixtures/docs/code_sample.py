"""Sample module used in smoke tests."""


def hello_world() -> str:
    return "hello world"


def calculate_sum(a: int, b: int) -> int:
    return a + b


class SearchClient:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def search(self, query: str) -> list[str]:
        return [query]
