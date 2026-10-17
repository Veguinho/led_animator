import base64
import json
import unittest
import zlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

import numpy as np

from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from doom_controls import DoomControls
from export_arduino import encode_rgb565
import stream_arduino


class DoomGameTests(unittest.TestCase):
    def test_doom_packets_wait_for_ack_without_an_extra_serial_drain(self):
        connection = mock.Mock()
        response = bytearray(stream_arduino.RESPONSE.pack(
            stream_arduino.RESPONSE_MAGIC, stream_arduino.PROTOCOL_VERSION,
            stream_arduino.STATUS_ACK, 0, 7))

        def read(size=1):
            result = bytes(response[:size])
            del response[:size]
            return result

        connection.read.side_effect = read
        stream_arduino.exchange_packet(connection, stream_arduino.PACKET_FRAME, 7,
                                       b"test frame", stream_arduino.STATUS_ACK, 1, 0, drain=False)
        connection.write.assert_called_once_with(stream_arduino.build_packet(
            stream_arduino.PACKET_FRAME, 7, b"test frame"))
        connection.flush.assert_not_called()
        self.assertEqual(response, bytearray())

    def test_later_ack_can_confirm_an_earlier_lost_doom_ack(self):
        connection = mock.Mock()
        response = bytearray(stream_arduino.RESPONSE.pack(
            stream_arduino.RESPONSE_MAGIC, stream_arduino.PROTOCOL_VERSION,
            stream_arduino.STATUS_ACK, 0, 8))

        def read(size=1):
            result = bytes(response[:size])
            del response[:size]
            return result

        connection.read.side_effect = read
        acknowledged = stream_arduino.exchange_packet(connection, stream_arduino.PACKET_FRAME, 7,
            b"older frame", stream_arduino.STATUS_ACK, 1, 0,
            drain=False, already_sent=True, allow_ack_ahead=True)
        self.assertEqual(acknowledged, 8)
        connection.write.assert_not_called()

    def test_frame_ownership_expiry_and_brightness(self):
        controls = DoomControls(16)
        with mock.patch("doom_controls.time.monotonic", return_value=10):
            session = controls.start()
            with self.assertRaisesRegex(ValueError, "already playing"):
                controls.start()
            payload = {"token": session["token"], "rgb": base64.b64encode(bytes([200]) * 768).decode()}
            controls.update(payload)
            np.testing.assert_array_equal(controls.frame(.25), np.full((16, 16, 3), 50, np.uint8))
            controls.stop({"token": "another browser"})
            self.assertTrue(controls.state()["active"])
            with self.assertRaises(ValueError):
                controls.update(payload | {"rgb": "bad frame"})
        with mock.patch("doom_controls.time.monotonic", return_value=13):
            self.assertIsNone(controls.frame(1))
            with self.assertRaisesRegex(ValueError, "expired"):
                controls.update(payload)
            replacement = controls.start()
            self.assertNotEqual(session["token"], replacement["token"])

    def test_usb_uses_latest_doom_frame_and_returns_to_original_source(self):
        controls = PaletteControls(size=16)
        controls.update(default_settings() | {"brightness": .5})
        session = controls.doom.start()
        first = np.full((16, 16, 3), 80, np.uint8)
        latest = np.full((16, 16, 3), 160, np.uint8)
        for frame in (first, latest):
            controls.doom.update({"token": session["token"], "rgb": base64.b64encode(frame.tobytes()).decode()})
        normal = encode_rgb565(np.full((16, 16, 3), 20, np.uint8))
        source = stream_arduino.FrameSource(1000, lambda: iter([normal, normal]), 16)
        sent = []

        def exchange(connection, packet_type, sequence, payload, *args, **kwargs):
            if packet_type in (stream_arduino.PACKET_FRAME, stream_arduino.PACKET_COMPRESSED_FRAME):
                sent.append(zlib.decompress(payload) if packet_type == stream_arduino.PACKET_COMPRESSED_FRAME else payload)
                controls.doom.stop({"token": session["token"]})

        with mock.patch.object(stream_arduino, "exchange_packet", side_effect=exchange):
            stream_arduino.stream_frames(mock.Mock(), source, loop=False, timeout=1, retries=0,
                                        drop_late=False, palette_controls=controls)
        self.assertEqual(sent, [encode_rgb565(first), encode_rgb565(first), normal, normal])

    def test_doom_stream_runs_at_30_fps_and_restores_the_source_rate(self):
        controls = PaletteControls(size=48)
        now = [0.0]
        sent, hello = [], []
        queued_at = {}
        connection = mock.Mock()

        def write(packet):
            header = stream_arduino.PACKET_HEADER.unpack_from(packet)
            queued_at[header[4]] = now[0]

        connection.write.side_effect = write
        source = stream_arduino.FrameSource(12, lambda: iter([bytes(48 * 48 * 2)]), 48)

        def exchange(connection, packet_type, sequence, payload, *args, **kwargs):
            if packet_type == stream_arduino.PACKET_HELLO:
                hello.append(stream_arduino.HELLO.unpack(payload))
            else:
                if kwargs.get("already_sent"):
                    # Model a 40 ms USB ACK round trip, longer than one frame.
                    now[0] = max(now[0], queued_at[sequence] + .04)
                sent.append(now[0])
                if len(sent) == 31:
                    controls.doom.stop({"token": session["token"]})

        with (mock.patch("time.monotonic", side_effect=lambda: now[0]),
              mock.patch.object(stream_arduino.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
              mock.patch.object(stream_arduino, "exchange_packet", side_effect=exchange)):
            session = controls.doom.start()
            controls.doom.update({"token": session["token"], "rgb": base64.b64encode(bytes([100]) * 48 * 48 * 3).decode()})
            stream_arduino.stream_frames(connection, source, loop=False, timeout=1, retries=0,
                                        drop_late=False, palette_controls=controls, display_size=48)
        self.assertAlmostEqual(sent[30] - sent[0], 1.0)
        self.assertEqual(hello[0][3:], (1, 33333))
        self.assertEqual(hello[1][3:], (0, 83333))
        self.assertEqual(controls.doom.state()["output_fps"], 30.0)

    def test_http_local_assets_frame_uploads_and_rejected_requests(self):
        server = PaletteServer(PaletteControls(size=48), 0, video_library={})
        server.start()
        self.addCleanup(server.close)

        def post(path, payload, **headers):
            req = Request(server.url + path, json.dumps(payload).encode(),
                          {"Content-Type": "application/json", **headers})
            with urlopen(req) as response:
                return json.load(response)

        with urlopen(server.url + "/doom/engine/chocolate-doom.wasm") as response:
            self.assertEqual(response.headers["Content-Type"], "application/wasm")
            self.assertEqual(response.read(4), b"\x00asm")
        with self.assertRaises(HTTPError) as error:
            urlopen(server.url + "/doom/%2e%2e/README.md")
        self.assertEqual(error.exception.code, 404)
        session = post("/api/doom/start", {})
        payload = {"token": session["token"], "rgb": base64.b64encode(bytes(48 * 48 * 3)).decode()}
        self.assertEqual(post("/api/doom/frame", payload), {"ok": True})
        with self.assertRaises(HTTPError) as error:
            post("/api/doom/frame", payload, Origin="https://example.com")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            post("/api/doom/frame", payload | {"token": "wrong session"})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(post("/api/doom/stop", {"token": session["token"]}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
