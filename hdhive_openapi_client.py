"""
HDHive 官方 OpenAPI facade。
"""
import logging

import hdhive_openapi_api as api

search_resources = api.search_resources
get_resources_by_tmdb_id = api.get_resources_by_tmdb_id
fetch_download_link = api.fetch_download_link
unlock_resource = api.unlock_resource
unlock_and_fetch = api.unlock_and_fetch
get_user_points = api.get_user_points

logger = logging.getLogger(__name__)
logger.info("🧭 HDHive 后端: official_openapi")
