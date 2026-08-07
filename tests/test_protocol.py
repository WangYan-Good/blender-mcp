import struct
import unittest

from blender_mcp.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    FrameTooLargeError,
    ProtocolError,
    encode_frame,
    make_request,
    receive_frame,
    validate_response,
)


class ChunkSocket:
    """Socket double that returns predetermined fragments."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
        return chunk[:size]


class ProtocolTests(unittest.TestCase):
    def test_fragmented_frame_round_trip_preserves_unicode(self):
        message = {
            "protocol_version": 1,
            "request_id": "abc",
            "success": True,
            "result": {"name": "人物"},
        }
        frame = encode_frame(message)

        result = receive_frame(
            ChunkSocket([frame[:1], frame[1:4], frame[4:9], frame[9:]])
        )

        self.assertEqual(result, message)

    def test_rejects_oversized_header_before_payload_read(self):
        socket = ChunkSocket([struct.pack("!I", MAX_FRAME_BYTES + 1)])

        with self.assertRaises(FrameTooLargeError):
            receive_frame(socket)

    def test_rejects_invalid_json_payload(self):
        payload = b"not-json"
        socket = ChunkSocket([struct.pack("!I", len(payload)), payload])

        with self.assertRaises(ProtocolError):
            receive_frame(socket)

    def test_rejects_connection_close_during_payload(self):
        socket = ChunkSocket([struct.pack("!I", 5), b"ab"])

        with self.assertRaisesRegex(ProtocolError, "connection closed"):
            receive_frame(socket)

    def test_request_has_known_id_and_protocol_version(self):
        request = make_request("get_status", {}, request_id="known")

        self.assertEqual(
            request,
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "known",
                "type": "get_status",
                "params": {},
            },
        )

    def test_response_request_id_must_match(self):
        response = {
            "protocol_version": 1,
            "request_id": "wrong",
            "success": True,
            "result": {},
        }

        with self.assertRaisesRegex(ProtocolError, "request_id"):
            validate_response(response, "expected")

    def test_response_protocol_version_must_match(self):
        response = {
            "protocol_version": 99,
            "request_id": "expected",
            "success": True,
            "result": {},
        }

        with self.assertRaisesRegex(ProtocolError, "protocol version"):
            validate_response(response, "expected")


if __name__ == "__main__":
    unittest.main()
