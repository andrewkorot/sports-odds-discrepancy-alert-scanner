from app.domain.models import CanonicalEvent, PredictionMarketQuote
from app.providers.mock.data import mock_snapshot


class MockKalshiAdapter:
    async def list_events(self) -> list[CanonicalEvent]:
        return [mock_snapshot()[0]]

    async def list_markets(self) -> list[PredictionMarketQuote]:
        return [mock_snapshot()[1][0]]

    async def get_market(self, market_id: str) -> PredictionMarketQuote:
        quote = (await self.list_markets())[0]
        if quote.provider_market_id != market_id:
            raise KeyError(market_id)
        return quote

    async def get_order_book(self, market_id: str) -> PredictionMarketQuote:
        return await self.get_market(market_id)

    async def get_quotes(self) -> list[PredictionMarketQuote]:
        return await self.list_markets()
