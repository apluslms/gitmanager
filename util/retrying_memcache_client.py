from pymemcache.client import retrying, hash
from django.core.cache.backends.memcached import PyMemcacheCache
from django.conf import settings

class RetryingMemcacheClient(PyMemcacheCache):
    def __init__(self, server, params):
        super().__init__(server, params)
        self._cache = retrying.RetryingClient(
            hash.HashClient(self.client_servers, **self._options),
            **settings.RETRYING_MEMCACHE_CLIENT_OPTIONS
        )
