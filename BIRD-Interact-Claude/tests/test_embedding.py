import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException
from openai import RateLimitError

from embedding import server


def _rate_limit_error(retry_after=None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"),
    )
    return RateLimitError("OpenAI rate limit exceeded", response=response, body=None)


class EmbeddingGatewayTests(unittest.TestCase):
    def setUp(self):
        server.get_embedder.cache_clear()

    def tearDown(self):
        server.get_embedder.cache_clear()

    def test_rate_limit_error_is_returned_as_429_with_retry_after(self):
        error = _rate_limit_error("7")

        with patch.object(server.Embedder, "encode", side_effect=error):
            with self.assertRaises(HTTPException) as raised:
                server.embed(server.EmbedRequest(texts=["hello"]))

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail, str(error))
        self.assertEqual(raised.exception.headers, {"Retry-After": "7"})

    def test_rate_limit_error_without_retry_after_still_returns_429(self):
        with patch.object(server.Embedder, "encode", side_effect=_rate_limit_error()):
            with self.assertRaises(HTTPException) as raised:
                server.embed(server.EmbedRequest(texts=["hello"]))

        self.assertEqual(raised.exception.status_code, 429)
        self.assertIsNone(raised.exception.headers)

    def test_other_embedding_runtime_errors_remain_503(self):
        with patch.object(
            server.Embedder,
            "encode",
            side_effect=RuntimeError("embedding backend failed"),
        ):
            with self.assertRaises(HTTPException) as raised:
                server.embed(server.EmbedRequest(texts=["hello"]))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "embedding backend failed")

    def test_success_response_is_unchanged(self):
        vectors = [[0.1, 0.2]]
        with patch.object(server.Embedder, "encode", return_value=vectors):
            response = server.embed(server.EmbedRequest(texts=["hello"]))

        self.assertEqual(
            response,
            {
                "embeddings": vectors,
                "model": server.MODEL_NAME,
                "dimensions": server.DIMENSIONS,
            },
        )

    def test_input_validation_remains_400(self):
        for request in (
            server.EmbedRequest(texts=[]),
            server.EmbedRequest(texts=["   "]),
        ):
            with self.subTest(request=request), self.assertRaises(HTTPException) as raised:
                server.embed(request)
            self.assertEqual(raised.exception.status_code, 400)

    def test_encode_does_not_wrap_rate_limit_error(self):
        error = _rate_limit_error("3")
        client = SimpleNamespace(
            embeddings=SimpleNamespace(create=Mock(side_effect=error))
        )
        embedder = server.Embedder()
        embedder._client = client

        with self.assertRaises(RateLimitError) as raised:
            embedder.encode(["hello"])
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
