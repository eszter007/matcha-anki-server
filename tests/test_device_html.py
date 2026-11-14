from matcha_anki_server.device_html import for_device


def test_keeps_content():
    html = '<div class="c"><b>和</b> hé<br><img src="a.png"></div>'
    assert for_device(html) == html


def test_drops_controls_icons_and_hidden():
    html = ('<div>和</div><a class="btn" onclick="go()"><div class="icon"><i class="material-icons">menu</i>'
            '</div></a><input type="checkbox"><button>OK</button><label>Grid</label>'
            '<div style="display:none">sidebar</div><audio src="x.mp3"></audio><!-- note -->')
    assert for_device(html) == "<div>和</div>"


def test_keeps_real_links_and_entities():
    html = '<a href="https://example.com">site</a> &amp; &#8226;'
    assert for_device(html) == html


def test_unclosed_inner_tag_in_dropped_element():
    assert for_device("<label><span>x</label><p>kept</p>") == "<p>kept</p>"
