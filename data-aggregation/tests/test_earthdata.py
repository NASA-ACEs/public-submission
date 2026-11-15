# fuel-lake — tests: shared Earthdata helpers (CMR parsing, NISAR names, coverage, auth, dB) and the probe
# Licensed under Apache-2.0.
from __future__ import annotations

import importlib.util
import io
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import h5py
import numpy as np
import pytest

from fuellake.config import Settings
from fuellake.connectors import _earthdata as ed
from fuellake.contracts import MixedMaturityError
from fuellake.registry import load_aoi

FIX = Path(__file__).parent / "fixtures" / "earthdata"
REPO = Path(__file__).resolve().parents[1]
GCOV_URL = ("https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/"
            "NISAR_L2_PR_GCOV_033_013_D_071_4005_DHDH_A_20261013T024033_20261013T024111_P05023_N_F_J_001/"
            "NISAR_L2_PR_GCOV_033_013_D_071_4005_DHDH_A_20261013T024033_20261013T024111_P05023_N_F_J_001.h5")
SAMPLE_SME2_URL = ("https://nisar.asf.earthdatacloud.nasa.gov/NISAR-SAMPLE-DATA/SME2/"
                   "NISAR_L3_PR_SME2_001_008_D_070_4000_QPNA_A_20190829T180759_20190829T180809_D00404_M_P_J_001/"
                   "NISAR_L3_PR_SME2_001_008_D_070_4000_QPNA_A_20190829T180759_20190829T180809_D00404_M_P_J_001.h5")


def load_fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def gcov_granules() -> list[ed.Granule]:
    return ed.parse_granules(load_fixture("cmr_gcov_provisional_smm.json")["items"])


# ---------------------------------------------------------------------------- UMM-G parsing
def test_parse_granule_gcov_fields():
    g = next(x for x in gcov_granules() if "013_D_071_4005_DHDH_A_20260615" in x.ur)
    assert g.concept_id.startswith("G") and g.collection == "NISAR_L2_GCOV_PROVISIONAL_V1"
    assert g.start == datetime(2026, 6, 15, 2, 40, 37, tzinfo=UTC)
    assert g.attr("STACK_ID") == "013_D_071" and g.attr("TRACK_NUMBER") == "13"
    assert g.attrs["FREQUENCY_A_POLARIZATION"] == ("HH", "HV")
    h5 = g.url_endswith(".h5")
    assert h5 and h5.startswith("https://nisar.asf.earthdatacloud.nasa.gov/") and "QA_STATS" not in h5
    assert g.size_of(h5) > 4_000_000_000          # GCOV files are 4.6-7.4 GB
    assert g.platform == "NISAR" and g.orbit_number and g.polygons and len(g.polygons[0]) == 5


def test_parse_time_truncates_long_fractions():
    assert ed._parse_time("2026-06-03T02:41:15.999342123Z") == datetime(2026, 6, 3, 2, 41, 15, 999342, tzinfo=UTC)


def test_parse_nisar_name():
    n = ed.parse_nisar_name(
        "NISAR_L2_PR_GCOV_031_013_D_071_4005_DHDH_A_20260919T024033_20260919T024111_P05023_F_F_J_001.h5")
    assert n["product"] == "GCOV" and n["processing"] == "PR" and n["level"] == 2
    assert (n["cycle"], n["track"], n["frame"]) == (31, 13, 71)
    assert n["orbit_state"] == "descending" and n["crid"] == "P05023" and n["counter"] == 1
    assert n["accuracy"] == "F" and n["coverage"] == "F" and n["stack_id"] == "013_D_071"
    assert n["primary_polarizations"] == ["HH", "HV"] and n["mode"] == "4005"
    sm = ed.parse_nisar_name(
        "NISAR_L3_PR_SME2_001_008_D_070_4000_QPNA_A_20190829T180759_20190829T180809_D00404_M_P_J_001")
    assert sm["product"] == "SME2" and sm["primary_polarizations"] == ["HH", "HV", "VV", "VH"]
    assert ed.parse_nisar_name("OPERA_L2_RTC-S1_T137-292315-IW3_20260602T015835Z_20260711T101639Z_S1A_30_v1.0") is None


def test_dedupe_latest_keeps_highest_counter():
    names = [ed.parse_nisar_name(g.ur) for g in gcov_granules()]
    kept = ed.dedupe_latest(names, key=lambda n: (n["stack_id"], n["start"]), version=lambda n: n["counter"])
    dup = [n for n in kept if n["stack_id"] == "114_D_071"]
    assert len(dup) == 1 and dup[0]["counter"] == 2
    assert len(kept) == len(names) - 1


def test_aoi_coverage_of_frames():
    smm = load_aoi("smm")
    by_stack = {g.attr("STACK_ID"): g for g in gcov_granules()}
    assert ed.aoi_coverage(ed.footprint(by_stack["013_D_071"]), smm) > 0.999
    assert 0.005 < ed.aoi_coverage(ed.footprint(by_stack["013_D_072"]), smm) < 0.03   # a sliver
    assert 0.9 < ed.aoi_coverage(ed.footprint(by_stack["114_D_071"]), smm) < 0.97
    assert ed.aoi_coverage(None, smm) == 0.0


# ---------------------------------------------------------------------------- CMR search
class _Resp:
    def __init__(self, items, headers):
        self._items, self.headers = items, headers

    def raise_for_status(self):
        pass

    def json(self):
        return {"items": self._items}


class _FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []
        self.headers: dict = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers or {})})
        return self.pages.pop(0)


def test_search_granules_pages_with_search_after():
    page1 = _Resp([{"n": 1}, {"n": 2}], {"CMR-Hits": "3", "CMR-Search-After": "[\"abc\"]"})
    page2 = _Resp([{"n": 3}], {"CMR-Hits": "3"})
    s = _FakeSession([page1, page2])
    out = ed.search_granules(s, "C2854338529-ASF", bbox=(-118.95, 33.98, -118.5, 34.2),
                             start=date(2026, 6, 1), end=date(2026, 11, 14), page_size=2)
    assert [o["n"] for o in out] == [1, 2, 3]
    p = s.calls[0]["params"]
    assert p["collection_concept_id"] == "C2854338529-ASF"
    assert p["bounding_box"] == "-118.95,33.98,-118.5,34.2"
    assert p["temporal"] == "2026-06-01T00:00:00Z,2026-11-14T23:59:59Z"
    assert s.calls[1]["headers"] == {"CMR-Search-After": "[\"abc\"]"}
    assert all("Authorization" not in c["headers"] for c in s.calls)


def test_cmr_session_sends_no_token(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-should-not-go-to-cmr")
    s = ed.cmr_session(Settings.from_env())
    assert s.headers["User-Agent"] == "fuel-lake/0.1" and "Authorization" not in s.headers


def test_check_single_maturity_refs():
    from fuellake.connectors.base import SourceRef

    mk = lambda m: SourceRef("nisar-gcov", f"k{m}", datetime(2026, 6, 1, tzinfo=UTC), meta={"maturity": m})  # noqa: E731
    assert ed.check_single_maturity_refs([mk("PROVISIONAL"), mk("PROVISIONAL")]) == {"PROVISIONAL"}
    with pytest.raises(MixedMaturityError):
        ed.check_single_maturity_refs([mk("PROVISIONAL"), mk("VALIDATED")])


# ---------------------------------------------------------------------------- arrays
def test_to_db():
    out = ed.to_db(np.array([1.0, 0.1, 0.01, 0.0, -1.0, np.nan, np.inf], dtype="float32"))
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[:3], [0.0, -10.0, -20.0], atol=1e-5)
    assert np.isnan(out[3:6]).all() and np.isnan(out[6])


def test_check_linear_power_refuses_db():
    ed.check_linear_power(np.array([0.01, 0.2, np.nan]), "ok")
    with pytest.raises(ValueError, match="linear power"):
        ed.check_linear_power(np.array([-12.0, -8.0, -15.0, 1.0]), "db-like")


def test_window_and_transform_from_centres():
    xc = 1000.0 + 10.0 * np.arange(100) + 5.0          # centres; edges on multiples of 10
    yc = 5000.0 - 10.0 * np.arange(50) - 5.0            # north-up, decreasing
    rs, cs = ed.window_from_centres(xc, yc, (1100.0, 4800.0, 1200.0, 4900.0))
    assert (cs.start, cs.stop) == (10, 20) and (rs.start, rs.stop) == (10, 20)
    rs2, cs2 = ed.window_from_centres(xc, yc, (1100.0, 4800.0, 1200.0, 4900.0), pad=20)
    assert (cs2.start, cs2.stop) == (8, 22)
    assert ed.window_from_centres(xc, yc, (0, 0, 10, 10)) is None
    t = ed.transform_from_centres(xc[0], yc[0], 10.0, -10.0)
    assert (t.c, t.f) == (1000.0, 5000.0)
    assert t @ (0.5, 0.5) == (xc[0], yc[0])


def test_fill_to_nan():
    out = ed.fill_to_nan(np.array([-9999.0, 0.2, np.nan]), -9999)
    assert np.isnan(out[0]) and out[1] == np.float32(0.2) and np.isnan(out[2])


# ---------------------------------------------------------------------------- auth
def test_require_token(monkeypatch):
    s = Settings.from_env()
    with pytest.raises(ed.EarthdataAuthError, match="EARTHDATA_TOKEN is not set"):
        ed.require_token(s, GCOV_URL)
    assert ed.require_token(s, SAMPLE_SME2_URL) is None           # public sample area
    assert ed.require_token(s, "https://cmr.earthdata.nasa.gov/search") is None
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-abc-123")
    assert ed.require_token(s, GCOV_URL) == "tok-abc-123"


def test_open_h5_local(tmp_path):
    p = tmp_path / "x.h5"
    with h5py.File(p, "w") as f:
        f["a"] = np.arange(3)
    with ed.open_h5(str(p), Settings.from_env()) as f:
        assert list(f["a"][()]) == [0, 1, 2]


class _FakeFS:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc = payload, exc

    def open(self, url, mode, block_size=None, cache_type=None):
        if self.exc:
            raise self.exc
        return io.BytesIO(self.payload)


def test_open_h5_detects_login_page(monkeypatch):
    import fsspec

    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-xyz-789")
    monkeypatch.setattr(fsspec, "filesystem", lambda *a, **k: _FakeFS(b"<!DOCTYPE html><html>login</html>"))
    with pytest.raises(ed.EarthdataAuthError, match="HTML login page"), ed.open_h5(GCOV_URL, Settings.from_env()):
        pass


def test_open_h5_error_is_redacted(monkeypatch):
    import fsspec

    token = "tok-secret-4567"
    monkeypatch.setenv("EARTHDATA_TOKEN", token)
    seen = {}

    def fake_filesystem(*a, **k):
        seen.update(k)
        return _FakeFS(exc=RuntimeError(f"401 for headers Authorization: Bearer {token}"))

    monkeypatch.setattr(fsspec, "filesystem", fake_filesystem)
    with pytest.raises(ed.EarthdataAuthError) as ei, ed.open_h5(GCOV_URL, Settings.from_env()):
        pass
    assert token not in str(ei.value) and "***" in str(ei.value)
    assert seen["headers"]["Authorization"] == f"Bearer {token}"
    assert seen["headers"]["User-Agent"] == "fuel-lake/0.1" and seen["skip_instance_cache"] is True


def test_download_html_raises(monkeypatch, tmp_path):
    import requests

    class R:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

    monkeypatch.setenv("EARTHDATA_TOKEN", "tok-1111")
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    url = "https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/OPERA_L2_RTC-S1/x/x_VV.tif"
    with pytest.raises(ed.EarthdataAuthError, match="HTML"):
        ed.download(url, str(tmp_path / "x.tif"), Settings.from_env())


# ---------------------------------------------------------------------------- probe script
def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_earthdata", REPO / "scripts" / "probe_earthdata.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Raw(io.BytesIO):
    def read(self, n=-1, decode_content=False):  # urllib3-like signature
        return super().read(n)


class _ProbeResp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code, self.headers = status, headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.raw = _Raw(body)

    def close(self):
        pass


def test_probe_reports_login_redirect_without_leaking_token(monkeypatch, capsys):
    import requests

    probe = _load_probe()
    token = "tok-probe-0001"
    sent = []

    def fake_get(self, url, headers=None, **kw):
        sent.append((url, dict(headers)))
        return _ProbeResp(302, {"Location": "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x&state=y"})

    monkeypatch.setattr(requests.Session, "get", fake_get)
    assert probe.probe(GCOV_URL, 1024, token) == 1
    out = capsys.readouterr().out
    assert "HTTP 302" in out and "login problem" in out and token not in out and "client_id" not in out
    assert sent[0][1]["Range"] == "bytes=0-1023" and sent[0][1]["Authorization"] == f"Bearer {token}"


def test_probe_success_and_token_only_to_origin(monkeypatch, capsys):
    import requests

    probe = _load_probe()
    token = "tok-probe-0002"
    sent = []
    responses = [_ProbeResp(307, {"Location": "https://d1.cloudfront.net/file.h5?Signature=zzz"}),
                 _ProbeResp(206, {"Content-Range": "bytes 0-1023/7398752256"}, ed.HDF5_SIGNATURE + b"\0" * 1016)]

    def fake_get(self, url, headers=None, **kw):
        sent.append((url, dict(headers)))
        return responses.pop(0)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    assert probe.probe(GCOV_URL, 1024, token) == 0
    out = capsys.readouterr().out
    assert "HTTP 307" in out and "HTTP 206" in out and "works" in out
    assert token not in out and "Signature" not in out
    assert "Authorization" in sent[0][1] and "Authorization" not in sent[1][1]


@pytest.mark.network
def test_probe_live_gcov_file():
    probe = _load_probe()
    code = probe.probe(GCOV_URL, 1024, os.environ.get("EARTHDATA_TOKEN") or None)
    assert code == (0 if os.environ.get("EARTHDATA_TOKEN") else 1)


@pytest.mark.network
def test_probe_live_public_sample():
    assert _load_probe().probe(SAMPLE_SME2_URL, 1024, None) == 0


def test_token_goes_only_to_earthdata_data_hosts():
    from fuellake.connectors._earthdata import wants_token

    assert wants_token("https://opendap.earthdata.nasa.gov/collections/C123-NSIDC_CPRD/granules/G.dap.nc4")
    assert wants_token("https://OPENDAP.earthdata.nasa.gov:443/collections/C/granules/G")
    assert wants_token("https://nisar.asf.earthdatacloud.nasa.gov/NISAR/x.h5")
    for url in ("http://opendap.earthdata.nasa.gov/collections/C/granules/G",            # not https
                "https://opendap.earthdata.nasa.gov.evil.example/x",                     # look-alike
                "https://evil.example/opendap.earthdata.nasa.gov",
                "https://opendap.earthdata.nasa.gov@evil.example/x",                     # userinfo trick
                "https://cmr.earthdata.nasa.gov/search/granules.umm_json",               # CMR is public
                "https://urs.earthdata.nasa.gov/oauth/authorize",
                "https://d1234.cloudfront.net/x.h5?Signature=1",                          # redirect target
                "https://nisar.asf.earthdatacloud.nasa.gov/NISAR-SAMPLE-DATA/x.h5"):      # public area
        assert not wants_token(url), url
