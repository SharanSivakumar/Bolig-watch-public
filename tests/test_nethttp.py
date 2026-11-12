"""Tests for nethttp module: IPv4 fallback for unrouted IPv6."""
import errno
import socket
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

import nethttp


def test_create_connection_v4_asks_getaddrinfo_for_af_inet_only(monkeypatch):
    """_create_connection_v4 must query getaddrinfo with AF_INET only."""
    gai_calls = []

    def _mock_gai(host, port, family, socktype):
        gai_calls.append((host, port, family, socktype))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    mock_sock = MagicMock()
    monkeypatch.setattr(socket, "getaddrinfo", _mock_gai)
    monkeypatch.setattr(socket, "socket", lambda af, st, proto: mock_sock)

    sock = nethttp._create_connection_v4(("example.com", 80))
    assert sock is mock_sock
    assert len(gai_calls) == 1
    assert gai_calls[0] == ("example.com", 80, socket.AF_INET, socket.SOCK_STREAM)
    mock_sock.connect.assert_called_once_with(("93.184.216.34", 80))


def test_create_connection_v4_raises_when_no_addresses(monkeypatch):
    """_create_connection_v4 must raise OSError when no IPv4 addresses exist."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(OSError, match="no IPv4 address for example.com"):
        nethttp._create_connection_v4(("example.com", 80))

    def _gai_error(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _gai_error)
    with pytest.raises(OSError, match="no IPv4 address for example.com"):
        nethttp._create_connection_v4(("example.com", 80))


def test_urlopen_returns_normal_result_single_attempt(monkeypatch):
    """Successful urlopen returns immediately and attempts request exactly once."""
    calls = []
    mock_resp = MagicMock()

    def _mock_urlopen(url, *a, **k):
        calls.append(url)
        return mock_resp

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)
    resp = nethttp.urlopen("https://example.com")
    assert resp is mock_resp
    assert len(calls) == 1


def test_urlopen_retries_over_ipv4_on_urlerror_enetunreach(monkeypatch):
    """urlopen retries once over IPv4 on URLError containing ENETUNREACH."""
    calls = []

    def _fail_unreach(url, *a, **k):
        calls.append(url)
        raise urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable"))

    mock_resp_v4 = MagicMock()
    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp_v4

    monkeypatch.setattr(urllib.request, "urlopen", _fail_unreach)
    monkeypatch.setattr(nethttp, "_v4_opener", lambda: mock_opener)

    resp = nethttp.urlopen("https://example.com/test", timeout=10)
    assert resp is mock_resp_v4
    assert len(calls) == 1
    mock_opener.open.assert_called_once_with("https://example.com/test", timeout=10)


def test_urlopen_retries_over_ipv4_on_bare_oserror(monkeypatch):
    """urlopen retries once over IPv4 on bare OSError with ENETUNREACH or EAFNOSUPPORT."""
    for err_no in (errno.ENETUNREACH, errno.EAFNOSUPPORT):
        calls = []

        def _fail_bare(url, *a, **k):
            calls.append(url)
            raise OSError(err_no, "Address family not supported")

        mock_resp_v4 = MagicMock()
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp_v4

        monkeypatch.setattr(urllib.request, "urlopen", _fail_bare)
        monkeypatch.setattr(nethttp, "_v4_opener", lambda: mock_opener)

        resp = nethttp.urlopen("https://example.com/bare")
        assert resp is mock_resp_v4
        assert len(calls) == 1
        mock_opener.open.assert_called_once_with("https://example.com/bare")


def test_urlopen_does_not_retry_timeout_httperror_or_econnrefused(monkeypatch):
    """urlopen must not retry timeout, HTTPError, or ECONNREFUSED."""
    mock_opener = MagicMock()
    monkeypatch.setattr(nethttp, "_v4_opener", lambda: mock_opener)

    # 1. TimeoutError
    def _fail_timeout(url, *a, **k):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", _fail_timeout)
    with pytest.raises(urllib.error.URLError):
        nethttp.urlopen("https://example.com/timeout")
    assert mock_opener.open.call_count == 0

    # 2. HTTPError
    def _fail_http(url, *a, **k):
        raise urllib.error.HTTPError("https://example.com/404", 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _fail_http)
    with pytest.raises(urllib.error.HTTPError):
        nethttp.urlopen("https://example.com/404")
    assert mock_opener.open.call_count == 0

    # 3. ECONNREFUSED
    def _fail_refused(url, *a, **k):
        raise urllib.error.URLError(OSError(errno.ECONNREFUSED, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", _fail_refused)
    with pytest.raises(urllib.error.URLError):
        nethttp.urlopen("https://example.com/refused")
    assert mock_opener.open.call_count == 0


def test_urlopen_re_raises_original_error_when_v4_retry_fails(monkeypatch):
    """When IPv4 fallback also fails, the original error is re-raised."""
    orig_err = urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable"))

    def _fail_unreach(url, *a, **k):
        raise orig_err

    mock_opener = MagicMock()
    mock_opener.open.side_effect = OSError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_unreach)
    monkeypatch.setattr(nethttp, "_v4_opener", lambda: mock_opener)

    with pytest.raises(urllib.error.URLError) as exc_info:
        nethttp.urlopen("https://example.com/retry-fail")

    assert exc_info.value is orig_err
    assert exc_info.value.reason.errno == errno.ENETUNREACH
