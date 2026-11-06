# Retries and deadlines

A transient rate limit can be retried after the server's Retry-After delay, if that delay fits the job's deadline. Exponential backoff with jitter reduces synchronized bursts. Limit the number of attempts so a slow provider cannot consume the entire worker pool.

A permanent authentication error calls for credential repair. Retrying an HTTP 401 with the same token usually repeats the failure. Refresh an expired access token once when the provider supports refresh.
