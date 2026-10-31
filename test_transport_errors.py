from curl_cffi.requests.exceptions import RequestException

import avito_client


def test_dns_timeout_is_a_transport_error() -> None:
    # curl 28 "Resolving timed out" arrives as a bare RequestException.
    assert issubclass(RequestException, avito_client.TRANSPORT_ERRORS)
