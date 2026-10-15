"""Launch Vine and request one physics step per displayed frame."""

import argparse
import math
import os
from pathlib import Path
import subprocess
import queue
import sys
import threading


WINDOW_SIZE = (1600, 1200)
# WINDOW_SIZE = (800, 600)
PIXELS_PER_UNIT = 40
# PIXELS_PER_UNIT = 20
ORIGIN_Y = 100



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
                shape, *fields = line.split()
                values = tuple(map(float, fields))
                field_counts = {"CIRCLE": (3,), "RECTANGLE": (4, 6)}.get(shape, ())
                if len(values) not in field_counts or not all(map(math.isfinite, values)):
                    raise ValueError
                sizes = values[2:3] if shape == "CIRCLE" else values[2:4]
                if any(size < 0 for size in sizes):
                    raise ValueError
            except ValueError:
                raise ValueError(
                    f"Line {line_number}: expected CIRCLE x y radius or "
                    "RECTANGLE x y half_width half_height [cos sin] (finite numbers, sizes >= 0)"
                ) from None
            bodies.append((shape, *values))
        else:
            raise ValueError(f"Line {line_number}: unexpected {line!r}")
    if bodies is not None:
        raise ValueError("Incomplete frame: missing END")


def draw_frame(pygame, screen, bodies):
    screen.fill((24, 26, 32))
    origin = (screen.get_width() // 2, ORIGIN_Y)
    scale = PIXELS_PER_UNIT
    pygame.draw.line(screen, (55, 58, 68), (0, origin[1]), (screen.get_width(), origin[1]))
    pygame.draw.line(screen, (55, 58, 68), (origin[0], 0), (origin[0], screen.get_height()))
    for shape, x, y, *size in bodies:
        center = (round(origin[0] + x * scale), round(origin[1] - y * scale))
        color = (130, 210, 160)
        if shape == "CIRCLE":
            pygame.draw.circle(screen, color, center, max(1, round(size[0] * scale)), 2)
        else:
            half_width, half_height = size[:2]
            cos, sin = size[2:] if len(size) == 4 else (1.0, 0.0)
            corners = []
            for local_x, local_y in (
                (-half_width, -half_height),
                (half_width, -half_height),
                (half_width, half_height),
                (-half_width, half_height),
            ):
                world_x = x + cos * local_x - sin * local_y
                world_y = y + sin * local_x + cos * local_y
                corners.append((
                    round(origin[0] + world_x * scale),
                    round(origin[1] - world_y * scale),
                ))
            pygame.draw.polygon(screen, color, corners, 2)


class Simulation:
    def __init__(self, binary):
        root = Path(__file__).resolve().parent.parent
        self.process = subprocess.Popen(
            [str(binary), "run", "booba/booba/"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
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
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("Vine did not exit after EOF; terminating it. Final stats may be unavailable.", file=sys.stderr)
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
    parser.add_argument("--vine", type=Path, default=Path(__file__).resolve().parent.parent / "target/release/vine")
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
        print(f"Cannot launch Vine: {error}. Build it with `cargo build --release --bin vine`.", file=sys.stderr)
        return 1

    try:
        pygame.display.init()
        screen = pygame.display.set_mode(WINDOW_SIZE)
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
    except KeyboardInterrupt:
        return 130
    finally:
        simulation.close()
        pygame.quit()


if __name__ == "__main__":
    raise SystemExit(main())
