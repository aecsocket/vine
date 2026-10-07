"""Launch Vine and request one physics step per displayed frame."""

import argparse
import math
import os
from pathlib import Path
import subprocess
import queue
import sys
import threading


def read_frames(stream):
    bodies = None
    for line_number, line in enumerate(stream, 1):
        line = line.strip()
        if not line:
            continue
        if line == "FRAME" and bodies is None:
            bodies = []
        elif line == "END" and bodies is not None:
            yield bodies
            bodies = None
        elif bodies is not None and line not in ("FRAME", "END"):
            try:
                x, y, radius = map(float, line.split())
                if not all(map(math.isfinite, (x, y, radius))) or radius < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Line {line_number}: expected finite x y radius (radius >= 0)") from None
            bodies.append((x, y, radius))
        else:
            raise ValueError(f"Line {line_number}: unexpected {line!r}")
    if bodies is not None:
        raise ValueError("Incomplete frame: missing END")


def draw_frame(pygame, screen, bodies):
    screen.fill((24, 26, 32))
    origin = (screen.get_width() // 2, 100)
    scale = 20
    pygame.draw.line(screen, (55, 58, 68), (0, origin[1]), (screen.get_width(), origin[1]))
    pygame.draw.line(screen, (55, 58, 68), (origin[0], 0), (origin[0], screen.get_height()))
    for x, y, radius in bodies:
        center = (round(origin[0] + x * scale), round(origin[1] - y * scale))
        pygame.draw.circle(screen, (130, 210, 160), center, max(1, round(radius * scale)), 2)


class Simulation:
    def __init__(self, binary):
        root = Path(__file__).resolve().parent.parent
        self.process = subprocess.Popen(
            [str(binary), "run", "booba/", "--no-stats", "--no-perf"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.frames = queue.Queue()
        self.reader = threading.Thread(target=self.receive, daemon=True)
        self.reader.start()

    def receive(self):
        try:
            for frame in read_frames(self.process.stdout):
                self.frames.put(frame)
        except (ValueError, OSError) as error:
            self.frames.put(error)
        finally:
            self.frames.put(None)

    def step(self):
        self.process.stdin.write("\n")
        self.process.stdin.flush()

    def close(self):
        # EOF releases Vine's read_line; terminate if it does not exit promptly.
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=1)
        self.process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vine", type=Path, default=Path(__file__).resolve().parent.parent / "target/debug/vine")
    parser.add_argument("--frames", type=int, help="Exit after this many displayed frames (for smoke tests)")
    args = parser.parse_args()
    if args.frames is not None and args.frames <= 0:
        parser.error("--frames must be positive")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        import pygame
    except ImportError:
        print("Install the viewer dependency: python3 -m pip install -r booba/requirements.txt", file=sys.stderr)
        return 1

    try:
        simulation = Simulation(args.vine.resolve())
    except OSError as error:
        print(f"Cannot launch Vine: {error}. Build it with `cargo build --bin vine`.", file=sys.stderr)
        return 1

    try:
        pygame.display.init()
        screen = pygame.display.set_mode((800, 600))
        clock = pygame.time.Clock()
        bodies = []
        waiting = True
        paused = False
        single_step = False
        displayed = 0
        while True:
            pygame.display.set_caption("Booba — " + ("paused" if paused else "running") + " (Space: pause, N: step, Esc: quit)")
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (
                    event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE
                ):
                    return 0
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_SPACE:
                        paused = not paused
                    elif event.key == pygame.K_n and paused:
                        single_step = True
            try:
                frame = simulation.frames.get_nowait()
            except queue.Empty:
                pass
            else:
                if isinstance(frame, Exception):
                    print(f"Invalid physics stream: {frame}", file=sys.stderr)
                    return 1
                if frame is None:
                    code = simulation.process.wait()
                    print(f"Vine exited (status {code}). See diagnostics above.", file=sys.stderr)
                    return code
                bodies = frame
                waiting = False
                displayed += 1
            draw_frame(pygame, screen, bodies)
            pygame.display.flip()
            clock.tick(60)
            if args.frames is not None and displayed >= args.frames:
                return 0
            if not waiting and (not paused or single_step):
                try:
                    simulation.step()
                except (BrokenPipeError, OSError) as error:
                    print(f"Cannot request physics step: {error}", file=sys.stderr)
                    return 1
                waiting = True
                single_step = False
    finally:
        simulation.close()
        pygame.quit()


if __name__ == "__main__":
    raise SystemExit(main())
