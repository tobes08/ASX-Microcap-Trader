import unittest
from unittest import mock

import requests

from openclaw import config, feed


class FakeResponse:
    def __init__(self, status, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append(dict(params or {}))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self):
        pass


def ok(items, count=None):
    return FakeResponse(200, {"data": {
        "items": items, "count": len(items) if count is None else count}})


class FetchQueryTests(unittest.TestCase):
    def run_with(self, script, call):
        session = FakeSession(script)
        with mock.patch.object(feed, "session", lambda: session), \
                mock.patch.object(feed, "reset_session", lambda: None), \
                mock.patch.object(feed.time, "sleep", lambda s: None):
            return call(), session

    def test_returns_items_and_count(self):
        data, session = self.run_with(
            [ok([{"documentKey": "A"}], count=7)],
            lambda: feed.fetch_query({"page": 0, "dateStart": "2026-10-01"}))
        self.assertEqual(data["count"], 7)
        self.assertEqual(data["items"], [{"documentKey": "A"}])
        self.assertEqual(session.calls[0]["dateStart"], "2026-10-01")

    def test_transient_errors_are_retried(self):
        data, session = self.run_with(
            [FakeResponse(502), requests.exceptions.ConnectionError("boom"),
             FakeResponse(200, None, bad_json=True), ok([{"documentKey": "A"}])],
            lambda: feed.fetch_query({"page": 0}, max_attempts=4))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(len(session.calls), 4)

    def test_gives_up_and_returns_none(self):
        data, session = self.run_with(
            [FakeResponse(503)] * 3,
            lambda: feed.fetch_query({"page": 0}, max_attempts=3))
        self.assertIsNone(data)
        self.assertEqual(len(session.calls), 3)

    def test_null_data_is_an_empty_result_not_a_crash(self):
        data, _ = self.run_with([FakeResponse(200, {"data": None})],
                                lambda: feed.fetch_query({"page": 0}))
        self.assertEqual(data["items"], [])

    def test_payload_of_the_wrong_shape_is_treated_as_bad_json(self):
        data, _ = self.run_with([FakeResponse(200, ["unexpected"])] * 2,
                                lambda: feed.fetch_query({"page": 0}, 2))
        self.assertIsNone(data)

    def test_fetch_page_keeps_its_contract(self):
        """The live poll relies on: a list on success, None on failure, and
        the configured page size on the wire."""
        items, session = self.run_with(
            [ok([{"documentKey": "A"}, {"documentKey": "B"}])],
            lambda: feed.fetch_page(3))
        self.assertEqual([i["documentKey"] for i in items], ["A", "B"])
        self.assertEqual(session.calls[0],
                         {"page": 3, "itemsPerPage": config.ITEMS_PER_PAGE})

        items, _ = self.run_with([FakeResponse(403)] * 2,
                                 lambda: feed.fetch_page(0, max_attempts=2))
        self.assertIsNone(items)


if __name__ == "__main__":
    unittest.main()
