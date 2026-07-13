import logging

logger = logging.getLogger(__name__)

# ruleid: encore.no-sensitive-values-in-logs
logger.info("plex token configured: %s", plex_token)

# ruleid: encore.no-sensitive-values-in-logs
logger.warning("taste profile: %s", taste_profile)

# ok: encore.no-sensitive-values-in-logs
logger.info("storage initialized")
