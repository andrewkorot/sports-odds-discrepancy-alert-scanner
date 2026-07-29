from app.domain.models import Bookmaker, CanonicalEvent, SportsbookQuote
from app.providers.mock.data import mock_snapshot


class MockOddsPapiAdapter:
    async def list_sports(self) -> list[str]:
        return ["soccer"]

    async def list_competitions(self) -> list[str]:
        return ["MLS"]

    async def list_events(self) -> list[CanonicalEvent]:
        return [mock_snapshot()[0]]

    async def list_bookmakers(self) -> list[Bookmaker]:
        return mock_snapshot()[3]

    async def get_event_odds(self, event_id: str) -> list[SportsbookQuote]:
        return mock_snapshot()[2]
