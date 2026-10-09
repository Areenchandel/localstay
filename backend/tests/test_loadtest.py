"""The load-test scenario itself, run in-process against the fake database."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import loadtest
from test_api import h, call  # noqa: F401


def test_scenario_one_winner_and_no_errors(h):
    post = lambda i, path, body: call(h, "POST " + path, body, sub=f"g{i}")
    host = lambda path, body: call(h, "POST " + path, body, sub="host")
    get = lambda path: call(h, "GET " + path)
    res = loadtest.scenario(post, get, host, guests=12, reads=30)
    assert res["won"] == 1 and res["rejected_409"] == 11 and res["other"] == 0 and res["read_errors"] == 0


def test_percentile():
    assert loadtest.percentile([1, 2, 3, 4, 100], 50) == 3
    assert loadtest.percentile([], 95) == 0.0
