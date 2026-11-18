from cards import cache


def test_once_reads_once_per_page_view():
    calls = []
    read = lambda: calls.append(1) or len(calls)
    assert cache.once("k", read) == 1 and cache.once("k", read) == 2   # outside a page view: every time
    with cache.request():
        assert cache.once("k", read) == 3 and cache.once("k", read) == 3
        cache.drop("k")   # written: read again
        assert cache.once("k", read) == 4
    assert cache.once("k", read) == 5   # the page view is over
