"""Tests for the Amazon PDP image/title extractor (pure HTML parsing)."""

from app.imgimport import amazon


def test_strip_size_token_and_thumb():
    full = amazon.strip_size_token("https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg")
    assert full == "https://m.media-amazon.com/images/I/71abc.jpg"
    assert amazon._thumb_from(full) == "https://m.media-amazon.com/images/I/71abc._AC_SX300_.jpg"


def test_extract_from_hires_block():
    # The colorImages 'initial' array carries hiRes URLs in inline JS (escaped slashes).
    html = """
    <html><head><title>x</title></head><body>
    <span id="productTitle">  OFF! Deep Woods  Insect Repellent </span>
    <script type="text/javascript">
    P.when('A').register("ImageBlockATF", function(A){
      var data = {
        'colorImages': { 'initial': [
          {"hiRes":"https:\\/\\/m.media-amazon.com\\/images\\/I\\/71aaa._AC_SL1500_.jpg","thumb":"https:\\/\\/m.media-amazon.com\\/images\\/I\\/71aaa._AC_US40_.jpg"},
          {"hiRes":"https:\\/\\/m.media-amazon.com\\/images\\/I\\/71bbb._AC_SL1500_.jpg","thumb":"x"}
        ] }
      };
    });
    </script>
    </body></html>
    """
    title, refs = amazon.extract_amazon_images(html)
    assert title == "OFF! Deep Woods Insect Repellent"
    assert [r["full"] for r in refs] == [
        "https://m.media-amazon.com/images/I/71aaa.jpg",
        "https://m.media-amazon.com/images/I/71bbb.jpg",
    ]
    # Each gets a derived lightweight thumbnail (never the full-res image).
    assert all("_AC_SX300_" in r["thumb"] for r in refs)


def test_extract_dedupes_by_full_res():
    # Two renditions of the same base image collapse to one.
    html = (
        '<span id="productTitle">T</span>'
        '"hiRes":"https://m.media-amazon.com/images/I/71z._AC_SL1500_.jpg"'
        '"hiRes":"https://m.media-amazon.com/images/I/71z._AC_SL1000_.jpg"'
    )
    _title, refs = amazon.extract_amazon_images(html)
    assert len(refs) == 1
    assert refs[0]["full"] == "https://m.media-amazon.com/images/I/71z.jpg"


def test_extract_falls_back_to_dynamic_image():
    # No hiRes/large — fall back to the landing image's data-a-dynamic-image map,
    # taking the largest by area. The attribute JSON is HTML-entity-encoded.
    html = (
        '<span id="productTitle">T</span>'
        '<img id="landingImage" data-a-dynamic-image="'
        '{&quot;https://m.media-amazon.com/images/I/61small._AC_SX300_.jpg&quot;:[300,300],'
        '&quot;https://m.media-amazon.com/images/I/61big._AC_SL1500_.jpg&quot;:[1500,1500]}" />'
    )
    _title, refs = amazon.extract_amazon_images(html)
    assert len(refs) == 1
    assert refs[0]["full"] == "https://m.media-amazon.com/images/I/61big.jpg"


def test_extract_empty_when_no_images():
    title, refs = amazon.extract_amazon_images("<html><body>nothing here</body></html>")
    assert refs == [] and title is None
