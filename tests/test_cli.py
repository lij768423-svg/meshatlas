from meshatlas.cli import local_url


def test_local_url():
    assert local_url("100.64.0.1", 8765) == "http://100.64.0.1:8765/healthz"
    assert local_url("0.0.0.0", 8765) == "http://127.0.0.1:8765/healthz"
    assert local_url("::", 9000) == "http://[::1]:9000/healthz"
    assert local_url("fd7a:115c:a1e0::1", 8765) == "http://[fd7a:115c:a1e0::1]:8765/healthz"
