"""Fixtures shared by the integration tests."""

from tests.integration.ws_server import threaded_server, threaded_server_bad_auth

__all__ = ["threaded_server", "threaded_server_bad_auth"]
