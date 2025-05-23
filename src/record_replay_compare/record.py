from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING
from typing import NamedTuple

import httpx

from record_replay_compare.wsgitools import start_http_server

if TYPE_CHECKING:
    from typing import Callable

    from typing_extensions import NotRequired
    from typing_extensions import TypeAlias
    from typing_extensions import TypedDict

    from record_replay_compare.wsgitools import JSON
    from record_replay_compare.wsgitools import HTTPRecordRequest
    from record_replay_compare.wsgitools import HTTPRecordResponse
    from record_replay_compare.wsgitools import InputArg
    from record_replay_compare.wsgitools import RequestHandler
    from record_replay_compare.wsgitools import WSGIResponse

    Comparator: TypeAlias = Callable[[HTTPRecordResponse, HTTPRecordResponse], HTTPRecordResponse]

    HTTPRequestHandler: TypeAlias = Callable[[HTTPRecordRequest], HTTPRecordResponse]

    class HttpClientConfig(TypedDict):
        base_url: str
        headers: NotRequired[dict[str, str]]


logger = logging.getLogger(__name__)


def http_request_adapter(
    func: HTTPRequestHandler,
) -> RequestHandler:
    """
    Adapter function to convert a function that takes in an InputArg and returns a WSGIResponse
    into a function that takes in an InputArg and returns a WSGIResponse.
    """

    def wrapper(event: InputArg) -> WSGIResponse:
        r = func(event.to_http_request())
        return {
            "status_code": r["status_code"],
            "headers": r["headers"],
            "body": iter([json.dumps(r["body"]).encode("utf-8")]),
        }

    return wrapper


class Recorder(NamedTuple):
    filename: str
    http_client: httpx.Client

    @staticmethod
    def normalize_body(response: httpx.Response) -> JSON:
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    def safe_caller(self, request_unit: HTTPRecordRequest) -> HTTPRecordResponse:
        try:
            response = self.http_client.request(**request_unit)
        except httpx.TransportError as e:
            return {
                "status_code": 500,
                "headers": {},
                "body": {"error": str(e)},
            }
        else:
            response_headers = {**response.headers}
            normalized_body = self.normalize_body(response)
            return {
                "status_code": response.status_code,
                "headers": response_headers,
                "body": normalized_body,
            }

    def __call__(self, /, request: HTTPRecordRequest) -> HTTPRecordResponse:
        response: HTTPRecordResponse = self.safe_caller(request)
        with open(self.filename, "a") as f:
            logger.info("Writing request/response to %s", self.filename)
            json.dump(
                {
                    "request": request,
                    "response": response,
                },
                f,
                indent=2,
            )
            f.write("\n")

        return response


class ParityChecker(NamedTuple):
    base: Recorder
    candidate: Recorder
    comparator: Comparator | None = None

    def __call__(self, /, request: HTTPRecordRequest) -> HTTPRecordResponse:
        base_response = self.base(request)
        candidate_response = self.candidate(request)
        if self.comparator is None:
            return base_response
        return self.comparator(base_response, candidate_response)


def main(argv: list[str] | None = None) -> int:
    """
    Main function to run the HTTP request handler.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Record and replay HTTP requests.",
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=27),
    )
    parser.add_argument(
        "base",
        type=str,
        help="Base url for base recorder.",
    )
    parser.add_argument(
        "--candidate",
        type=str,
        default=None,
        required=False,
        help="Base url for candidate recorder.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to run the HTTP server on.",
    )
    parser.add_argument(
        "--output-dir", type=str, default=".", help="Output directory for the recorder files."
    )

    args = parser.parse_args(argv)
    base_recorder = Recorder(
        filename=os.path.join(args.output_dir, "base.json"),
        http_client=httpx.Client(base_url=args.base),
    )
    final: HTTPRequestHandler = base_recorder
    if args.candidate:
        candidate_recorder = Recorder(
            filename=os.path.join(args.output_dir, "candidate.json"),
            http_client=httpx.Client(base_url=args.candidate),
        )
        final = ParityChecker(base_recorder, candidate_recorder)
        logger.info("Using parity checker with base %s and candidate %s", args.base, args.candidate)
    else:
        logger.info("Using base recorder with base %s", args.base)

    http_request_handler: RequestHandler = http_request_adapter(final)

    start_http_server(http_request_handler, host="localhost", port=args.port)
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
