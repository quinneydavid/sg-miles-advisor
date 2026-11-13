from cards import awards


def test_chart_prices_from_singapore():
    lhr = awards.prices("LHR", "J")
    assert lhr == {"zone": 11, "zone_name": "Europe", "saver": 108500, "advantage": 141500}
    assert awards.prices("HND", "F")["saver"] == 81000 and awards.prices("HND", "Y")["advantage"] == 49500
    assert awards.prices("DPS", "W") == {"zone": 2, "zone_name": "Malaysia, Indonesia and Brunei", "saver": None, "advantage": None}
    assert awards.prices("SYD", "W")["advantage"] is None and awards.prices("SYD", "W")["saver"] == 53500
    assert awards.prices("YVR", "J") is None
    c = awards.chart()
    assert all(len(v) == 12 for lvl in c["from_sin"].values() for v in lvl.values())
    assert all(str(z) in c["zones"] for z in c["airport_zone"].values())


def test_access_only_when_above_advantage():
    p = awards.prices("HND", "J")
    assert awards.access_seen(p, 104000) == 104000 and awards.access_seen(p, 80500) is None and awards.access_seen(p, None) is None


def test_unchecked_cards_get_the_usual_exclusions():
    from cards import cache
    u = cache.usual_exclusions()
    assert {"6300", "4900", "6540", "9399", "8211", "8250"} <= u and "5812" not in u
