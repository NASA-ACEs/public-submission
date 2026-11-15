# fuel-lake — first-hour Earthdata check: ranged GET of one real NISAR GCOV file with EARTHDATA_TOKEN
# Licensed under Apache-2.0.
"""Prove that an Earthdata Login bearer token can read NISAR science files.

Usage::

    uv run --env-file .env python scripts/probe_earthdata.py            # default GCOV file (track 13, frame 71)
    uv run python scripts/probe_earthdata.py <https-url> --bytes 1024

It sends ``GET`` with ``Range: bytes=0-<n-1>`` and follows redirects by hand, printing one line per hop
(status, host, path; query strings are dropped because signed URLs carry credentials). The bearer token is
read from ``EARTHDATA_TOKEN``, sent only to the original data host, and never printed.

How to read the result:

- ``206`` with an HDF5 signature: the token works.
- ``302`` to ``urs.earthdata.nasa.gov/oauth/authorize``: no token, or the token was not accepted.
- ``401`` / ``403``: token rejected (expired, or the account has not authorized the ASF application).

Exit code 0 when the bytes read are HDF5, 1 otherwise, 2 on a network error.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urljoin, urlsplit

import requests

USER_AGENT = "fuel-lake/0.1"
DEFAULT_URL = (
    "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/"
    "NISAR_L2_PR_GCOV_033_013_D_071_4005_DHDH_A_20261013T024033_20261013T024111_P05023_N_F_J_001/"
    "NISAR_L2_PR_GCOV_033_013_D_071_4005_DHDH_A_20261013T024033_20261013T024111_P05023_N_F_J_001.h5"
)
HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
MAX_HOPS = 10


def _hop_label(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if len(parts.path) <= 90 else parts.path[:40] + "..." + parts.path[-40:]
    return f"{parts.netloc}{path}"


def probe(url: str, n_bytes: int = 1024, token: str | None = None, timeout: float = 60.0) -> int:
    """Run the probe and print the status chain. Returns the process exit code."""
    origin = urlsplit(url).netloc
    session = requests.Session()
    current = url
    try:
        for hop in range(MAX_HOPS):
            headers = {"User-Agent": USER_AGENT, "Range": f"bytes=0-{n_bytes - 1}"}
            if token and urlsplit(current).netloc == origin:
                headers["Authorization"] = f"Bearer {token}"
            resp = session.get(current, headers=headers, allow_redirects=False, timeout=timeout, stream=True)
            print(f"  hop {hop}: HTTP {resp.status_code}  {_hop_label(current)}")
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                resp.close()
                nxt = urljoin(current, location)
                if urlsplit(nxt).netloc.endswith("urs.earthdata.nasa.gov"):
                    print(f"  -> redirect to Earthdata Login ({_hop_label(nxt)})")
                    print("RESULT: login problem (302 to URS): token missing, expired, or not accepted for this host.")
                    return 1
                current = nxt
                continue
            body = resp.raw.read(n_bytes, decode_content=True) if resp.status_code in (200, 206) else b""
            resp.close()
            if resp.status_code in (200, 206):
                is_hdf5 = body[:8] == HDF5_SIGNATURE
                print(f"  read {len(body)} bytes; HDF5 signature: {'yes' if is_hdf5 else 'no'}"
                      f"; Content-Range: {resp.headers.get('Content-Range', '-')}")
                if is_hdf5:
                    print("RESULT: works (ranged read of a NISAR HDF5 file succeeded).")
                    return 0
                print("RESULT: unexpected content (not HDF5): probably an HTML login page.")
                return 1
            if resp.status_code in (401, 403):
                print(f"RESULT: token rejected (HTTP {resp.status_code}).")
            else:
                print(f"RESULT: unexpected HTTP {resp.status_code}.")
            return 1
        print("RESULT: too many redirects.")
        return 1
    except requests.RequestException as exc:
        print(f"RESULT: network error: {type(exc).__name__}")
        return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="HTTPS URL of a NISAR .h5 file")
    ap.add_argument("--bytes", type=int, default=1024, help="number of bytes to request (default 1024)")
    args = ap.parse_args(argv)
    token = os.environ.get("EARTHDATA_TOKEN") or None
    print(f"EARTHDATA_TOKEN: {'set' if token else 'not set'}")
    print(f"probe: GET bytes=0-{args.bytes - 1} {_hop_label(args.url)}")
    return probe(args.url, args.bytes, token)


if __name__ == "__main__":
    sys.exit(main())
