from shelf.wacz import WaczFile

from .conftest import COVER_BYTES, COVER_URL, build_wacz


def test_reads_metadata_pages_and_records(wacz_path):
    with WaczFile(wacz_path) as w:
        assert w.title == "Night Train Archive"
        assert len(w.pages()) == 6
        resp = w.get("https://example.com/novel/chapter-1/")
        assert resp.status == 200 and resp.mime == "text/html"
        assert b"End of chapter 1." in resp.body
        assert w.get(COVER_URL).body == COVER_BYTES
        assert w.get("https://missing.example/") is None


def test_falls_back_to_scanning_warcs_without_index(tmp_path):
    path = build_wacz(tmp_path / "noindex.wacz", with_index=False)
    with WaczFile(path) as w:
        assert w.best_capture("https://example.com/novel/chapter-4-2/").status == 404
        assert b"End of chapter 2." in w.get("https://example.com/novel/chapter-2/").body
