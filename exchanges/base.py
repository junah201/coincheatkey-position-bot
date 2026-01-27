from abc import ABC, abstractmethod


class ExchangeWebSocket(ABC):
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key

    @abstractmethod
    async def start(self):
        pass
