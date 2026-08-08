import json
import logging
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


class TimeoutSocket(ScriptedSocket):
    def recv(self, _size):
        raise TimeoutError("Blender command timed out")


class ConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection_logger = logging.getLogger("BlenderMCPConnection")
        cls.previous_disabled = cls.connection_logger.disabled
        cls.connection_logger.disabled = True

    @classmethod
    def tearDownClass(cls):
        cls.connection_logger.disabled = cls.previous_disabled

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

    def test_timeout_has_stable_category_request_id_and_invalidates_socket(self):
        socket = TimeoutSocket(b"")
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "timeout-request",
        )

        with self.assertRaises(BlenderCommandError) as caught:
            connection.send_command("render", {"width": 1024, "height": 1024})

        self.assertEqual(caught.exception.category, "TIMEOUT")
        self.assertEqual(caught.exception.request_id, "timeout-request")
        # Blender may still be completing a timed-out mutation, so callers
        # must not automatically replay it.
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(socket.closed)

    def test_malformed_success_is_protocol_error_with_request_id(self):
        response = encode_frame(
            {
                "protocol_version": 1,
                "request_id": "fixed",
                "success": True,
                "result": [],
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
            connection.send_command("get_status")

        self.assertEqual(caught.exception.category, "PROTOCOL_ERROR")
        self.assertEqual(caught.exception.request_id, "fixed")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(socket.closed)

    def test_local_logs_never_include_script_body(self):
        marker = "PRIVATE_SCRIPT_MARKER_7f1d6b"
        response = encode_frame(
            {
                "protocol_version": 1,
                "request_id": "fixed",
                "success": True,
                "result": {"success": True},
            }
        )
        socket = ScriptedSocket(response)
        connection = BlenderConnection(
            "localhost",
            9876,
            socket_factory=lambda *args: socket,
            request_id_factory=lambda: "fixed",
        )

        self.connection_logger.disabled = False
        try:
            with self.assertLogs("BlenderMCPConnection", level="INFO") as captured:
                connection.send_command(
                    "execute_python", {"code": f"result = '{marker}'"}
                )
        finally:
            self.connection_logger.disabled = True

        self.assertNotIn(marker, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
