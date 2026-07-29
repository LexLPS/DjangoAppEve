"""Schema generation hooks."""


def only_v1_endpoints(endpoints, **kwargs):
    """Document just the versioned API.

    The legacy unversioned `/api/profile/` route still exists for old
    callers, but it is not part of the published contract — including it
    would also collide with the v1 Profile component.
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if path.startswith("/api/v1/")
    ]
