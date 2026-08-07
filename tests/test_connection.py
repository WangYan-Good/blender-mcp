import json
import struct
import unittest

from blender_mcp.connection import BlenderCommandError, BlenderConnection
from blender_mcp.protocol import encode_frame


class ScriptedSocket:
    def __init__(self, response_frame):
        self.remaining = response_frame
        self.sent = []
        self.closed = False
        self.connect_calls = []
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.connect_calls.append(address)

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        chunk, self.remaining = self.remaining[:size], self.remaining[size:]
        return chunk

    def close(self):
        self.closed = True


class ConnectionTests(unittest.TestCase):
    def test_success_returns_result_and_sends_one_framed_request(self):
        response = encode_frame(
            {
                "protocol_version": 1,
                "request_id": "fixed",
                "success": True,
                "result": {"ready": True},
            }
        )
        socket = ScriptedSocket(response)
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "fixed",
        )

        result = connection.send_command("get_status")

        self.assertEqual(result, {"ready": True})
        self.assertEqual(len(socket.sent), 1)
        payload_size = struct.unpack("!I", socket.sent[0][:4])[0]
        request = json.loads(socket.sent[0][4 : 4 + payload_size])
        self.assertEqual(request["type"], "get_status")
        self.assertEqual(request["request_id"], "fixed")

    def test_remote_error_preserves_diagnostics_and_connection(self):
        response = encode_frame(
            {
                "protocol_version": 1,
                "request_id": "fixed",
                "success": False,
                "error": {
                    "category": "BLENDER_ERROR",
                    "type": "KeyError",
                    "message": "missing",
                    "traceback": "trace",
                    "retryable": False,
                    "details": {"stdout": "before"},
                },
            }
        )
        socket = ScriptedSocket(response)
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "fixed",
        )

        with self.assertRaises(BlenderCommandError) as caught:
            connection.send_command("execute_python", {"code": "bad"})

        self.assertEqual(caught.exception.category, "BLENDER_ERROR")
        self.assertEqual(caught.exception.error_type, "KeyError")
        self.assertEqual(caught.exception.traceback_text, "trace")
        self.assertEqual(caught.exception.details["stdout"], "before")
        self.assertFalse(socket.closed)

    def test_broken_receive_invalidates_socket_without_replaying_mutation(self):
        socket = ScriptedSocket(b"")
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "fixed",
        )

        with self.assertRaises(Exception):
            connection.send_command("save", {"path": "projects/a.blend"})

        self.assertEqual(len(socket.sent), 1)
        self.assertTrue(socket.closed)
        self.assertIsNone(connection.sock)

    def test_mismatched_response_invalidates_connection(self):
        response = encode_frame(
            {
                "protocol_version": 1,
                "request_id": "stale",
                "success": True,
                "result": {},
            }
        )
        socket = ScriptedSocket(response)
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "fixed",
        )

        with self.assertRaisesRegex(Exception, "request_id"):
            connection.send_command("get_status")

        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
