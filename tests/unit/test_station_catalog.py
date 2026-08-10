from partsouq_crawler.services.station_catalog import NhtsaVinDecoder


def test_vin_decoder_preserves_final_response_headers_and_redacts_cookie() -> None:
    headers = NhtsaVinDecoder._parse_response_headers(  # noqa: SLF001 - parser contract
        b"HTTP/1.1 200 Connection established\r\n\r\n"
        b"HTTP/2 200\r\nContent-Type: application/json\r\n"
        b"X-Trace: first\r\nX-Trace: second\r\nSet-Cookie: secret=1\r\n\r\n"
    )

    assert headers == {
        "content-type": "application/json",
        "set-cookie": "[redacted]",
        "x-trace": "first, second",
    }
