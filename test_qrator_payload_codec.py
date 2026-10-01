import json
import re
from pathlib import Path

from qrator_payload_codec import decode_field, encode_field


ROOT = Path(__file__).resolve().parent


def _captured_fields() -> tuple[dict, dict[str, str]]:
    trace = json.loads((ROOT / "qrator-fingerprint-trace.json").read_text(encoding="utf-8"))
    body = trace["submittedFtRequest"]["body"]
    fields = {
        name: re.search(
            rf'name="{name}"\r\n\r\n([^\r]+)',
            body,
        ).group(1)
        for name in ("f", "s")
    }
    return trace, fields


def test_f_matches_bundle_and_decodes_to_raw_browser_vector() -> None:
    trace, fields = _captured_fields()
    raw = trace["cipherCalls"][0]["raw"]

    assert encode_field(raw, "f") == fields["f"]
    assert decode_field(fields["f"], "f") == raw


def test_s_matches_bundle_and_decodes_to_extension_state_json() -> None:
    trace, fields = _captured_fields()
    raw = trace["cipherCalls"][1]["raw"]

    assert encode_field(raw, "s") == fields["s"]
    assert decode_field(fields["s"], "s") == raw


def test_camoufox_f_round_trip() -> None:
    trace = json.loads((ROOT / "qrator-camoufox-trace.json").read_text(encoding="utf-8"))
    fields = trace["ftRequests"][0]["fields"]

    assert decode_field(fields["f"], "f") == trace["decoded"]["f"]
    assert encode_field(trace["decoded"]["f"], "f") == fields["f"]


def test_camoufox_s_round_trip() -> None:
    trace = json.loads((ROOT / "qrator-camoufox-trace.json").read_text(encoding="utf-8"))
    fields = trace["ftRequests"][0]["fields"]

    assert decode_field(fields["s"], "s") == trace["decoded"]["s"]
    assert encode_field(trace["decoded"]["s"], "s") == fields["s"]
