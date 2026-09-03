"""I/O shell: OAuth, the shared rate-limited HTTP session, the API client, the cache.

Nothing in this package knows anything about deduplication. It speaks only in terms
of "the saved-album listing", "album ids to delete", "album ids to save", so later
features reuse it unchanged.
"""
