from cards import catalogue

PAGE = b"""<h3>Kris+ Merchants by Earn Rate</h3><table><tr><td>Esso</td><td>9 mpd</td></tr></table>
<h3>All Kris+ Merchants alphabetical (A-Z)</h3><table><tr><td><strong>Merchant</strong></td><td>Earn<br/>Rate</td></tr>
<tr><td><small>8 Korean BBQ (The Central)</small></td><td><strong>2 mpd</strong></td></tr>
<tr><td><small>A&amp;S Lighting</small></td><td><strong>1.5 mpd</strong></td></tr>
<tr><td><small>Esso</small></td><td><strong>2 mpd</strong></td></tr></table>"""


def test_parse_kris_reads_the_alphabetical_table_only():
    assert catalogue.parse_kris(PAGE) == [("8 Korean BBQ (The Central)", 2.0), ("A&S Lighting", 1.5), ("Esso", 2.0)]
    assert catalogue.parse_kris(b"<p>nothing</p>") == []


def test_kris_key_drops_the_outlet():
    assert catalogue.kris_key("8 Korean BBQ (The Central)") == "8-korean-bbq"
    assert catalogue.kris_key("Marks & Spencer Singapore") == "marks-spencer"
