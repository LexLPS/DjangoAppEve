from .base import *

DEBUG = True
ALLOWED_HOSTS = []

# Browsable API is a dev convenience only; production stays JSON-only
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}