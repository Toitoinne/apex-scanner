from apex.features.service import pg_text


def test_pg_text_retire_octet_nul():
    assert pg_text("PE\x00PE\x00") == "PEPE"
    assert pg_text("") == ""
    assert pg_text(None) is None
    assert pg_text("normal") == "normal"
