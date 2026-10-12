import io

from pathlib import Path
import queue
import unittest

from viewer import Simulation, draw_frame, read_frames


class ProtocolTests(unittest.TestCase):
    def test_frames(self):
        frames = list(read_frames(io.StringIO(
            "FRAME\nCIRCLE 1 -2 3\nRECTANGLE 4 5 0.5 2\nEND\nFRAME\nEND\n"
        )))
        self.assertEqual(frames, [[("CIRCLE", 1.0, -2.0, 3.0), ("RECTANGLE", 4.0, 5.0, 0.5, 2.0)], []])

    def test_rectangle_rotation(self):
        frames = list(read_frames(io.StringIO(
            "FRAME\nRECTANGLE 0 0 2 1 0 -1\nEND\n"
        )))
        self.assertEqual(frames, [[("RECTANGLE", 0.0, 0.0, 2.0, 1.0, 0.0, -1.0)]])

    def test_invalid_streams(self):
        for stream in (
            "CIRCLE 1 2 3\n", "END\n", "FRAME\nFRAME\n", "FRAME\nCIRCLE 1 2\nEND\n",
            "FRAME\nCIRCLE nan 2 3\nEND\n", "FRAME\nCIRCLE 1 2 -3\nEND\n", "FRAME\nCIRCLE 1 2 3\n",
            "FRAME\nRECTANGLE 1 2 3\nEND\n", "FRAME\nRECTANGLE 1 2 -3 4\nEND\n",
            "FRAME\nRECTANGLE 1 2 3 -4\nEND\n", "FRAME\nRECTANGLE 1 2 3 inf\nEND\n",
            "FRAME\nTRIANGLE 1 2 3\nEND\n", "FRAME\nCIRCLE 1 2 3 4\nEND\n",
            "FRAME\nRECTANGLE 0 0 1 2 0\nEND\n",
            "FRAME\nRECTANGLE 0 0 1 2 nan 1\nEND\n",
        ):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                list(read_frames(io.StringIO(stream)))


class RenderingTests(unittest.TestCase):
    def test_circle_and_rectangle_outlines(self):
        try:
            import pygame
        except ImportError:
            self.skipTest("Install viewer requirements for rendering tests")
        screen = pygame.Surface((800, 600))
        draw_frame(pygame, screen, [
            ("CIRCLE", 0.0, 0.0, 1.0),
            ("RECTANGLE", 3.0, 2.0, 1.0, 0.5),
        ])
        self.assertEqual(screen.get_at((419, 100))[:3], (130, 210, 160))
        self.assertEqual(screen.get_at((440, 50))[:3], (130, 210, 160))
        self.assertEqual(screen.get_at((480, 70))[:3], (130, 210, 160))
        self.assertEqual(screen.get_at((460, 60))[:3], (24, 26, 32))


    def test_rotated_rectangle_outline(self):
        try:
            import pygame
        except ImportError:
            self.skipTest("Install viewer requirements for rendering tests")
        screen = pygame.Surface((800, 600))
        draw_frame(pygame, screen, [("RECTANGLE", 0.0, 0.0, 2.0, 1.0, 0.0, 1.0)])
        color = (130, 210, 160)
        self.assertEqual(screen.get_at((400, 60))[:3], color)
        self.assertEqual(screen.get_at((420, 100))[:3], color)
        self.assertEqual(screen.get_at((440, 90))[:3], (24, 26, 32))
        self.assertEqual(screen.get_at((400, 100))[:3], (55, 58, 68))


class HandshakeTests(unittest.TestCase):
    def test_vine_waits_for_each_step(self):
        binary = Path(__file__).resolve().parent.parent / "target/debug/vine"
        if not binary.exists():
            self.skipTest("Build Vine to run the handshake integration test")
        simulation = Simulation(binary)
        try:
            first = simulation.frames.get(timeout=5)
            self.assertIsInstance(first, list)
            self.assertEqual(first[0][:3], ("CIRCLE", 0.0, 0.0))
            self.assertGreater(first[0][3], 0.0)
            with self.assertRaises(queue.Empty):
                simulation.frames.get(timeout=0.1)
            simulation.step()
            second = simulation.frames.get(timeout=5)
            self.assertIsInstance(second, list)
            self.assertLess(second[0][2], first[0][2])
            with self.assertRaises(queue.Empty):
                simulation.frames.get(timeout=0.1)
        finally:
            simulation.close()
        self.assertEqual(simulation.process.returncode, 0)
        self.assertFalse(simulation.reader.is_alive())


if __name__ == "__main__":
    unittest.main()
