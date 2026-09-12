"""Middleware of the public catalog process.

The stack order and the settings live in ``public_settings``; this module holds
the three public-only middleware classes.
"""

import logging
import time

from django.db import DatabaseError
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers

from . import public_seo
from .public_settings import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY

access_logger = logging.getLogger("apps.catalog.public.access")
error_logger = logging.getLogger("apps.catalog.public")


class PublicAccessLogMiddleware:
    """One line per request: route, status and latency. No query, no body, no PII.

    The operational formatter adds the request ID, method and path (never the
    query string). Search text, cart contents and cookies are not logged.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        route = match.url_name if match and match.url_name else "unmatched"
        if request.path.startswith("/static/"):
            route = "static"
        if not (route == "public_catalog_healthz" and response.status_code == 200):
            access_logger.info(
                "route=%s status=%s ms=%.1f",
                route,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
        return response


class PublicResponsePolicyMiddleware:
    """Headers every public response carries, decided in one place.

    * Content-Security-Policy: the pages load only same-origin styles,
      images and scripts. The only script is the phone mask on the request
      form; inline script, external hosts and eval stay forbidden.
    * X-Robots-Tag: ``noindex, nofollow`` unless indexing is switched on.
    * Cache-Control: HTML shows live price, stock and the visitor's own cart,
      so it is never stored by a shared cache. Views that are safe to cache
      (photos, robots, sitemaps) set their own policy, which is kept.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        if not public_seo.indexing_enabled():
            response["X-Robots-Tag"] = "noindex, nofollow"
        content_type = response.get("Content-Type", "")
        if content_type.startswith("text/html") and not response.has_header("Cache-Control"):
            add_never_cache_headers(response)
            response["Cache-Control"] += ", private"
        return response


class PublicDatabaseUnavailableMiddleware:
    """A database failure becomes a calm 503 page, never an error dump.

    Only the exception class is logged: the message can contain SQL text or
    connection details.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not isinstance(exception, DatabaseError):
            return None
        error_logger.error("database unavailable: %s", type(exception).__name__)
        response = render(
            request, "public_catalog/errors/unavailable.html", {"indexing": False}, status=503
        )
        response["Retry-After"] = "30"
        add_never_cache_headers(response)
        return response
