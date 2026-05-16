from __future__ import annotations

from config import AUTO_UNLOCK_THRESHOLD
from hdhive_openapi_state import HDHiveOpenAPIState
from hdhive_openapi_flow_unlock import HDHiveOpenAPIUnlockFlow
from hdhive_openapi_flow_symedia import HDHiveOpenAPISymediaFlow
from hdhive_openapi_flow_search import HDHiveOpenAPISearchFlow


class HDHiveOpenAPIFlowService:
    def __init__(self):
        self.state = HDHiveOpenAPIState()
        self.unlock = HDHiveOpenAPIUnlockFlow(self.state)
        self.symedia = HDHiveOpenAPISymediaFlow(self.state)
        self.search = HDHiveOpenAPISearchFlow(self.state)

        self.unlock.link_extracted_handler = self.symedia.handle_link_extracted
        self.unlock.auto_unlock_threshold = AUTO_UNLOCK_THRESHOLD
        self.search.unlock_required_handler = self.unlock.handle_unlock_required
        self.search.fetch_result_handler = self.unlock.fetch_download_link_and_handle_result

        self.pending_sa_tasks = self.state.pending_sa_tasks
        self.resource_website_cache = self.state.resource_website_cache
        self.resource_list_state = self.state.resource_list_state
        self.tmdb_search_state = self.state.tmdb_search_state

    def make_message_state_key(self, message):
        return self.state.make_message_state_key(message)

    async def cancel_latest_sa_task(self, message):
        return await self.symedia.cancel_latest_sa_task(message)

    async def auto_add_to_sa(self, task_key, link, original_message, countdown=60):
        return await self.symedia.auto_add_to_sa(task_key, link, original_message, countdown=countdown)

    async def handle_send_to_sa_callback(self, callback):
        return await self.symedia.handle_send_to_sa_callback(callback)

    async def handle_search_input(self, message, user_input, search_type):
        return await self.search.handle_search_input(message, user_input, search_type)

    async def handle_resource_link(self, message, resource_id, resource_url=None):
        return await self.search.handle_resource_link(message, resource_id, resource_url)

    async def handle_tmdb_link(self, message, tmdb_id, media_type):
        return await self.search.handle_tmdb_link(message, tmdb_id, media_type)

    async def handle_keyword_search(self, message, keyword, search_type):
        return await self.search.handle_keyword_search(message, keyword, search_type)

    async def handle_provider_filter_callback(self, callback):
        return await self.search.handle_provider_filter_callback(callback)

    async def handle_tmdb_page_callback(self, callback):
        return await self.search.handle_tmdb_page_callback(callback)

    async def handle_resource_callback(self, callback):
        return await self.search.handle_resource_callback(callback)

    async def handle_direct_link_message(self, message):
        return await self.search.handle_direct_link_message(message)

    async def handle_select_tmdb_callback(self, callback):
        return await self.search.handle_select_tmdb_callback(callback)

    async def handle_unlock_callback(self, callback):
        return await self.unlock.handle_unlock_callback(callback)

    async def handle_cancel_unlock_callback(self, callback):
        return await self.unlock.handle_cancel_unlock_callback(callback)


hdhive_openapi_flow_service = HDHiveOpenAPIFlowService()
