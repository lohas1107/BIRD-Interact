import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


MODULE_PATH = Path(__file__).with_name("import_embeddings.py")
SPEC = importlib.util.spec_from_file_location("import_embeddings", MODULE_PATH)
assert SPEC and SPEC.loader
import_embeddings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(import_embeddings)


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://embedding.test/embed")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("embedding request failed", request=request, response=response)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return next(self.responses)


class ImportEmbeddingsTests(unittest.TestCase):
    def test_default_batch_size_is_eight(self):
        self.assertEqual(import_embeddings.DEFAULT_BATCH_SIZE, 8)

    def test_retries_rate_limit_and_honors_retry_after(self):
        client = _Client([
            _Response(429, headers={"retry-after": "0.25"}),
            _Response(200, payload={"embeddings": [[1.0]], "model": "test-model"}),
        ])

        with patch.object(import_embeddings.time, "sleep") as sleep:
            payload = import_embeddings._request_embeddings(
                client,
                "http://embedding.test",
                ["text"],
                offset=0,
                max_retries=1,
            )

        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(len(client.calls), 2)
        sleep.assert_called_once_with(0.25)

    def test_retries_gateway_503_with_exponential_backoff(self):
        client = _Client([
            _Response(503),
            _Response(503),
            _Response(200, payload={"embeddings": [[1.0]], "model": "test-model"}),
        ])

        with patch.object(import_embeddings.time, "sleep") as sleep:
            import_embeddings._request_embeddings(
                client,
                "http://embedding.test",
                ["text"],
                offset=8,
                max_retries=2,
            )

        self.assertEqual(sleep.call_args_list[0].args, (1.0,))
        self.assertEqual(sleep.call_args_list[1].args, (2.0,))


if __name__ == "__main__":
    unittest.main()
