import io
import math
from pathlib import Path
import queue
import unittest

from viewer import Simulation, read_frames


class ProtocolTests(unittest.TestCase):
    def test_frames(self):
        frames = list(read_frames(io.StringIO("FRAME\n1 -2 3\n4 5 0.5\nEND\nFRAME\nEND\n")))
        self.assertEqual(frames, [[(1.0, -2.0, 3.0), (4.0, 5.0, 0.5)], []])

    def test_invalid_streams(self):
        for stream in (
            "1 2 3\n", "END\n", "FRAME\nFRAME\n", "FRAME\n1 2\nEND\n",
            "FRAME\nnan 2 3\nEND\n", "FRAME\n1 2 -3\nEND\n", "FRAME\n1 2 3\n",
        ):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                list(read_frames(io.StringIO(stream)))


class HandshakeTests(unittest.TestCase):
    def test_vine_waits_for_each_step(self):
        binary = Path(__file__).resolve().parent.parent / "target/debug/vine"
        if not binary.exists():
            self.skipTest("Build Vine to run the handshake integration test")
        simulation = Simulation(binary)
        try:
            first = simulation.frames.get(timeout=5)
            self.assertIsInstance(first, list)
            self.assertEqual(first[0][:2], (0.0, 0.0))
            self.assertTrue(math.isclose(first[0][2], 3.14, rel_tol=1e-6))
            with self.assertRaises(queue.Empty):
                simulation.frames.get(timeout=0.1)
            simulation.step()
            second = simulation.frames.get(timeout=5)
            self.assertIsInstance(second, list)
            self.assertLess(second[0][1], first[0][1])
            with self.assertRaises(queue.Empty):
                simulation.frames.get(timeout=0.1)
        finally:
            simulation.close()
        self.assertEqual(simulation.process.returncode, 0)
        self.assertFalse(simulation.reader.is_alive())


if __name__ == "__main__":
    unittest.main()
