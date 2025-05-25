from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import overload
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server
from wsgiref.util import application_uri
from wsgiref.util import request_uri

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator
    from typing import Callable
    from typing import Final
    from typing import Literal
    from typing import TypedDict
    from typing import TypeVar
    from wsgiref.types import ErrorStream
    from wsgiref.types import InputStream

    from _typeshed.wsgi import StartResponse
    from _typeshed.wsgi import WSGIApplication
    from _typeshed.wsgi import WSGIEnvironment
    from typing_extensions import NotRequired
    from typing_extensions import Self
    from typing_extensions import TypeAlias

    HTTP_METHOD = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"]
    JSON: TypeAlias = 'int | float | str | bool | None | "list[JSON]" | "dict[str, JSON]"'

    class HTTPRecordRequest(TypedDict):
        method: HTTP_METHOD
        url: str
        params: NotRequired[Mapping[str, list[str]]]
        headers: NotRequired[Mapping[str, str]]
        json: NotRequired[JSON]

    class HTTPRecordResponse(TypedDict):
        status_code: int
        headers: Mapping[str, str]
        body: JSON

    class WSGIResponse(TypedDict):
        status_code: int
        headers: Mapping[str, str]
        body: Iterable[bytes]

    class WSGIVars(TypedDict):
        version: tuple[int, int]
        url_scheme: str
        input: InputStream
        errors: ErrorStream
        multithread: bool
        multiprocess: bool
        run_once: bool

    class CGIVars(TypedDict):
        REQUEST_METHOD: HTTP_METHOD
        SCRIPT_NAME: str
        PATH_INFO: str
        QUERY_STRING: str
        CONTENT_TYPE: NotRequired[str]
        CONTENT_LENGTH: NotRequired[str]
        SERVER_NAME: str
        SERVER_PORT: int | str
        SERVER_PROTOCOL: str

    T = TypeVar("T")

    InputArg: TypeAlias = "ParsedWsgi"
    # InputArg: TypeAlias = "SimpleRequestEvent"
    RequestHandler = Callable[[InputArg], WSGIResponse]


class HttpHeaders(Mapping[str, str]):
    def __init__(self, /, items: Mapping[str, str]) -> None:
        self._data: dict[str, tuple[str, str]] = {k.lower(): (k, v) for k, v in items.items()}

    def __getitem__(self, key: str) -> str:
        return self._data[key.lower()][1]

    def __iter__(self) -> Iterator[str]:
        return (k for k, _ in self._data.values())

    def __len__(self) -> int:
        return len(self._data)


@dataclass(frozen=True)
class ParsedWsgi:
    headers: HttpHeaders
    wsgi_vars: WSGIVars
    cgi_vars: CGIVars
    other: Mapping[str, str]
    environ: WSGIEnvironment

    @classmethod
    def from_environ(cls, environ: WSGIEnvironment) -> Self:
        headers: dict[str, str] = {}
        wsgi_vars: WSGIVars = {}  # type: ignore[typeddict-item]
        cgi_vars: CGIVars = {}  # type: ignore[typeddict-item]
        other = {}

        for key, value in environ.items():
            if os.environ.get(key) == value:
                continue
            if key.startswith("HTTP_"):
                headers[key[5:].replace("_", "-")] = value
            elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                cgi_vars[key] = value  # type: ignore[literal-required]
                # headers[key.replace("_", "-")] = value
            elif key.startswith("wsgi."):
                wsgi_vars[key[5:]] = value  # type: ignore[literal-required]
            elif key in (
                "REQUEST_METHOD",
                "SCRIPT_NAME",
                "PATH_INFO",
                "QUERY_STRING",
                "CONTENT_TYPE",
                "CONTENT_LENGTH",
                "SERVER_NAME",
                "SERVER_PORT",
                "SERVER_PROTOCOL",
            ):
                cgi_vars[key] = value  # type: ignore[literal-required]
            else:
                other[key] = value
        headers.pop("HOST", None)
        return cls(
            headers=HttpHeaders(headers),
            wsgi_vars=wsgi_vars,
            cgi_vars=cgi_vars,
            other=other,
            environ=environ,
        )

    @cached_property
    def method(self) -> HTTP_METHOD:
        return self.cgi_vars["REQUEST_METHOD"]

    @cached_property
    def path(self) -> str:
        return self.cgi_vars["PATH_INFO"]

    @cached_property
    def params(self) -> dict[str, list[str]]:
        return parse_qs(self.cgi_vars["QUERY_STRING"])

    @cached_property
    def content(self) -> bytes:
        return self.wsgi_vars["input"].read(int(self.cgi_vars.get("CONTENT_LENGTH", "0") or "0"))

    @cached_property
    def full_url(self) -> str:
        return request_uri(self.environ)

    @cached_property
    def base_url(self) -> str:
        return application_uri(self.environ)

    def to_http_request(self) -> HTTPRecordRequest:
        return {
            "method": self.method,
            "url": self.path,
            "params": self.params,
            "headers": dict(self.headers.items()),
            "json": json.loads(self.content) if self.content else None,
        }


def _remove_hop_by_hop_headers(headers: Mapping[str, str]) -> dict[str, str]:
    # Need to remove hop-by-hop headers
    # https://github.com/python/cpython/blob/24b147a19b360c49cb1740aa46211d342aaa071f/Lib/wsgiref/util.py#L151
    hoppish_headers = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
        # This is not a hop-by-hop header, but it is a header that should not be sent in the response
        "content-length",
    }
    return {k: v for k, v in headers.items() if k.lower() not in hoppish_headers}


def create_simple_wsgi_app(
    request_handler: RequestHandler,
) -> WSGIApplication:
    """
    Creates a simple WSGI application using the provided request handler.

    Args:
        request_handler (RequestHandler):
            A function that takes an InputArg object and returns a SimpleResponseEvent.

    Returns:
        WSGIApplication: A WSGI-compatible application callable.

    The generated WSGI application extracts request data from the WSGI environment,
    passes it to the request handler, and constructs an HTTP response using the
    returned status code, headers, and body. Hop-by-hop headers are removed from
    the response headers before sending the response.
    """

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        arg = ParsedWsgi.from_environ(environ)
        response_event = request_handler(arg)
        status_code = response_event["status_code"]
        phrase = HTTPStatus(status_code).phrase
        headers = _remove_hop_by_hop_headers(response_event["headers"])
        start_response(f"{status_code} {phrase}", list(headers.items()))
        return response_event["body"]

    return app


def serve_forever(
    request_handler: RequestHandler,
    host: str = "127.0.0.1",
    port: int = 3000,
) -> None:
    """
    Starts a simple HTTP server using the provided request handler.

    Args:
        request_handler (RequestHandler):
            A callable that processes incoming HTTP requests and returns responses.
        host (str, optional):
            The hostname or IP address to bind the server to. Defaults to "127.0.0.1".
        port (int, optional):
            The port number to listen on. Defaults to 3000.

    Raises:
        SystemExit:
            Raised when the server is interrupted by a KeyboardInterrupt (Ctrl+C).

    Logs:
        Logs the URL where the server is running.
    """
    with make_server(host, port, create_simple_wsgi_app(request_handler)) as httpd:
        sa = httpd.socket.getsockname()
        server_host, server_port = sa[0], sa[1]
        logger.info("Running on http://%s:%d", server_host, server_port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            raise SystemExit(0) from None


DEFAULT_RESPONSE: Final[WSGIResponse] = {
    "status_code": 200,
    "headers": {},
    "body": (b"",),
}


@overload
def handle_request(
    request_handler: RequestHandler,
    host: str = ...,
    port: int = ...,
    timeout: float | None = ...,
    response_factory: Callable[[WSGIResponse], WSGIResponse] | None = None,
) -> WSGIResponse | None: ...


@overload
def handle_request(
    request_handler: Callable[[InputArg], T],
    host: str = ...,
    port: int = ...,
    timeout: float | None = ...,
    response_factory: Callable[[T], WSGIResponse] = ...,
) -> T | None: ...


def handle_request(
    request_handler: Callable[[InputArg], T],
    host: str = "127.0.0.1",
    port: int = 3000,
    timeout: float | None = None,
    response_factory: Callable[[T], WSGIResponse] | None = None,
) -> T | None:
    """
    Serves a single HTTP request using a simple WSGI server.

    Parameters:
        request_handler (Callable[[InputArg], T]):
            A function that processes the incoming request event and returns a response object of type T.
        host (str, optional):
            The hostname or IP address to bind the server to. Defaults to "127.0.0.1".
        port (int, optional):
            The port number to listen on. Defaults to 3000.
        timeout (float | None, optional):
            The maximum time in seconds to wait for a request before timing out. If None, waits indefinitely.
        response_factory (Callable[[T], SimpleResponseEvent], optional):
            A function that converts the result from the request handler into a SimpleResponseEvent.
            Defaults to a lambda returning DEFAULT_RESPONSE.

    Returns:
        T | None:
            The result returned by the request_handler for the handled request, or None if no request was handled.

    Raises:
        SystemExit:
            If a KeyboardInterrupt is received during request handling.
    """  # noqa: E501
    # Use a list to allow the inner function to modify the value in the enclosing scope
    ret: list[T | None] = [None]

    def inner(event: InputArg) -> WSGIResponse:
        result = request_handler(event)
        ret.append(result)
        if response_factory is None:
            return DEFAULT_RESPONSE
        return response_factory(result)

    with make_server(host, port, create_simple_wsgi_app(inner)) as wsgi_server:
        wsgi_server.timeout = timeout
        sa = wsgi_server.socket.getsockname()
        server_host, server_port = sa[0], sa[1]
        logger.info("Running on http://%s:%d", server_host, server_port)
        try:
            wsgi_server.handle_request()
        except KeyboardInterrupt:
            raise SystemExit(0) from None
    return ret[-1]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def example_handler(_event: InputArg) -> WSGIResponse:
        # _event.wsgi_vars["input"].__iter__
        # event = _event.to_simple_request_event()
        return {
            "status_code": 200,
            "headers": {
                "Content-Type": "application/json",
            },
            "body": (json.dumps(_event, default=str).encode("utf-8"),),
        }

    e = handle_request(example_handler, response_factory=lambda _: DEFAULT_RESPONSE)
    serve_forever(example_handler)

    # import uvicorn
    # uvicorn.run(
    #     interface="wsgi", app=create_simple_wsgi_app(example_handler), host="127.0.0.1", port=3000
    # )

    # import gunicorn.app.base
    # class StandaloneApplication(gunicorn.app.base.BaseApplication):
    #     def load_config(self):
    #         ...
    #     def load(self):
    #         return create_simple_wsgi_app(example_handler)

    # StandaloneApplication().run()
