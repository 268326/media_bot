from __future__ import annotations

from aiogram.types import Message

RESOURCE_WEBSITE_CACHE_LIMIT = 2048
RESOURCE_LIST_STATE_LIMIT = 256
TMDB_SEARCH_STATE_LIMIT = 256
TMDB_PAGE_SIZE = 5


class HDHiveOpenAPIState:
    def __init__(self):
        self.pending_sa_tasks: dict[str, dict] = {}
        self.resource_website_cache: dict[str, str] = {}
        self.resource_list_state: dict[str, dict] = {}
        self.tmdb_search_state: dict[str, dict] = {}

    @staticmethod
    def trim_dict_cache(cache: dict, limit: int) -> None:
        while len(cache) > limit:
            oldest_key = next(iter(cache))
            cache.pop(oldest_key, None)

    @staticmethod
    def make_message_state_key(message: Message | None) -> str | None:
        if not message:
            return None
        chat = getattr(message, "chat", None)
        if not chat:
            return None
        return f"{chat.id}:{message.message_id}"

    def cache_resource_websites(self, resources: list[dict]) -> None:
        for res in resources:
            rid = str(res.get("id") or "")
            website = str(res.get("website") or "")
            if rid and website:
                self.resource_website_cache[rid] = website
                self.trim_dict_cache(self.resource_website_cache, RESOURCE_WEBSITE_CACHE_LIMIT)

    def save_resource_list_state(self, message: Message | None, resources: list[dict], media_type: str, title: str | None = None) -> None:
        state_key = self.make_message_state_key(message)
        if not state_key:
            return
        self.resource_list_state[state_key] = {
            "resources": resources,
            "media_type": media_type,
            "title": title,
        }
        self.trim_dict_cache(self.resource_list_state, RESOURCE_LIST_STATE_LIMIT)

    def save_tmdb_search_state(self, message: Message | None, results: list[dict], page: int = 0) -> None:
        state_key = self.make_message_state_key(message)
        if not state_key:
            return
        self.tmdb_search_state[state_key] = {
            "results": results,
            "page": page,
        }
        self.trim_dict_cache(self.tmdb_search_state, TMDB_SEARCH_STATE_LIMIT)
