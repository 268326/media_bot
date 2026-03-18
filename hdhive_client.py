"""
HDHive API client facade (Open API).
"""
import logging

from hdhive_http_api import (  # type: ignore
    search_resources,
    get_resources_by_tmdb_id,
    fetch_download_link,
    unlock_and_fetch,
    get_user_points,
)

logger = logging.getLogger(__name__)
logger.info("🧭 HDHive 后端: open_api")
