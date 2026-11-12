"""HTTP helper for BoligWatch v3.2: single IPv4 fallback on unrouted IPv6.

When a host has no IPv6 default route, urllib fails instantly on dual-stack
hosts publishing AAAA records with ENETUNREACH (101) or EAFNOSUPPORT (97).
This module retries the request once forced to IPv4 (AF_INET only) before
counting a failure against any circuit breaker.
"""
import errno
import http.client
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import config

_cached_v4_opener: Optional[urllib.request.OpenerDirector] = None


def _create_connection_v4(address: tuple, timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
                          source_address: Optional[tuple] = None) -> socket.socket:
    """Create a TCP connection forcing IPv4 (AF_INET only)."""
    host, port = address
    err = None
    try:
        results = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"no IPv4 address for {host}") from exc

    for res in results:
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError(f"no IPv4 address for {host}")


class _V4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_v4


class _V4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_v4


class _V4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_V4HTTPConnection, req)


class _V4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_V4HTTPSConnection, req, context=self._context)


def _v4_opener() -> urllib.request.OpenerDirector:
    """Return a memoised OpenerDirector using IPv4-only connections."""
    global _cached_v4_opener
    if _cached_v4_opener is None:
        _cached_v4_opener = urllib.request.build_opener(
            _V4HTTPHandler(),
            _V4HTTPSHandler(),
        )
    return _cached_v4_opener


def _extract_host(req: Any) -> str:
    if isinstance(req, urllib.request.Request):
        return req.host or urllib.parse.urlparse(req.full_url).hostname or str(req.full_url)
    if isinstance(req, str):
        return urllib.parse.urlparse(req).hostname or req
    return str(req)


def _is_unreachable(err: BaseException) -> bool:
    target_errnos = (errno.ENETUNREACH, errno.EAFNOSUPPORT)
    if isinstance(err, urllib.error.URLError) and isinstance(err.reason, OSError):
        return err.reason.errno in target_errnos
    if isinstance(err, OSError) and not isinstance(err, urllib.error.HTTPError):
        return err.errno in target_errnos
    return False


def urlopen(url: Any, data: Optional[bytes] = None,
            timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
            **kwargs: Any) -> Any:
    """urllib.request.urlopen with a single IPv4 fallback on ENETUNREACH/EAFNOSUPPORT.

    On dual-stack hosts when this machine has no IPv6 route, urllib fails with
    errno 101/97. We catch that specific condition and retry once forced to IPv4.
    If the retry fails, the original error is re-raised.
    """
    try:
        if data is None:
            if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                return urllib.request.urlopen(url, **kwargs)
            return urllib.request.urlopen(url, timeout=timeout, **kwargs)
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            return urllib.request.urlopen(url, data=data, **kwargs)
        return urllib.request.urlopen(url, data=data, timeout=timeout, **kwargs)
    except Exception as orig_err:
        if not _is_unreachable(orig_err):
            raise
        host = _extract_host(url)
        if config.VERBOSE:
            print(f"  [nethttp] IPv4 fallback for {host}")
        opener = _v4_opener()
        try:
            if data is None:
                if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                    return opener.open(url)
                return opener.open(url, timeout=timeout)
            if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                return opener.open(url, data=data)
            return opener.open(url, data=data, timeout=timeout)
        except Exception:
            raise orig_err from None


def main() -> None:
    import subprocess
    r = subprocess.run(["ip", "-6", "route", "show", "default"], capture_output=True, text=True)
    print(f"IPv6 default route: {r.stdout.strip() or 'none'}")
    for url in config.OVERPASS_URLS:
        h = urllib.parse.urlparse(url).hostname
        a = [res[4][0] for res in socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM)]
        try:
            aaaa = [res[4][0] for res in socket.getaddrinfo(h, None, socket.AF_INET6, socket.SOCK_STREAM)]
        except Exception:
            aaaa = []
        print(f"DNS {h}: A={sorted(set(a))}, AAAA={sorted(set(aaaa))}")
    try:
        with urllib.request.urlopen("https://example.com", timeout=5) as r:
            print(f"Control (https://example.com): HTTP {r.status}")
    except Exception as e:
        print(f"Control (https://example.com): failed: {e}")
    for url in config.OVERPASS_URLS:
        s_url = url.replace("/interpreter", "/status")
        h = urllib.parse.urlparse(s_url).hostname
        req_v4 = urllib.request.Request(s_url, headers={"User-Agent": "BoligWatch/3.0"})
        req_def = urllib.request.Request(s_url, headers={"User-Agent": "BoligWatch/3.0"})
        try:
            with urllib.request.urlopen(req_def, timeout=5) as r: d_res = f"HTTP {r.status}"
        except Exception as e: d_res = str(e)
        try:
            with _v4_opener().open(req_v4, timeout=5) as r: v4_res = f"HTTP {r.status}"
        except Exception as e: v4_res = str(e)
        try:
            addrs = socket.getaddrinfo(h, 443 if s_url.startswith("https") else 80, socket.AF_INET6, socket.SOCK_STREAM)
            if not addrs: v6_res = "no AAAA"
            else:
                s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s.settimeout(5); s.connect(addrs[0][4]); s.close()
                v6_res = "connected"
        except Exception as e: v6_res = str(e)
        print(f"Status {s_url}:\n  default: {d_res}\n  IPv4:    {v4_res}\n  IPv6:    {v6_res}")


if __name__ == "__main__":
    main()
