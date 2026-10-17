#![no_std]
#![no_main]

mod graphics;

use core::{cell::RefCell, fmt::Write, mem};

use anyhow::{Context, Result, ensure};
use embedded_graphics::{
  prelude::{Point, Primitive, Size},
  primitives::{Circle, Line, PrimitiveStyle, Rectangle},
};
use embedded_graphics_core::{
  Drawable,
  pixelcolor::Rgb888,
  prelude::{DrawTarget, RgbColor},
};
use ivm::{
  host::{
    Host, IVM,
    ext::{ExtFn, ExtTyRegister, common},
    runner::Runner,
  },
  runtime::{NoopHooks, ext::ExtTyCastStatic, heap::Heap, word::Word},
};
use log::{error, info};
use uefi::{prelude::*, proto::console::gop::GraphicsOutput};
use vine_util::register::Register;

use crate::graphics::GopDisplay;

/// Single-threaded UEFI app, so a `RefCell` global is fine; `GopDisplay` is
/// only `Send`-able by fiat since nothing else touches the framebuffer.
struct Global<T>(RefCell<T>);
unsafe impl<T> Sync for Global<T> {}

static DISPLAY: Global<Option<GopDisplay>> = Global(RefCell::new(None));

#[entry]
fn main() -> Status {
  uefi::helpers::init().unwrap();
  match run() {
    Ok(()) => {
      info!("success!!");
      loop {}
    }
    Err(err) => {
      error!("error: {err:?}");
      Status::LOAD_ERROR
    }
  }
}

fn run() -> Result<()> {
  let gop_handle =
    uefi::boot::get_handle_for_protocol::<GraphicsOutput>().context("no graphics output")?;
  let mut gop = uefi::boot::open_protocol_exclusive::<GraphicsOutput>(gop_handle)
    .context("opening graphics protocol")?;
  let display = GopDisplay::new(&mut gop)?;
  mem::forget(gop);
  DISPLAY.0.replace(Some(display));

  // setup copied from `ivm_cli.rs`
  let src_contents = include_str!("physics.iv");
  let table = &mut ivy::name::Table::default();
  let nets = ivy::text::parser::Parser::parse(table, &src_contents).context("parsing program")?;
  let nets = nets.to_flat_nets().context("creating flat nets")?;

  // setup copied from `RunArgs::run`
  let mut heap = Heap::with_size(256 * 1024 * 1024).context("system memory is too small")?;
  let mut ivm = IVM::new();
  let mut host = Host::new(&mut ivm);
  let extrinsics = (common::all(), io_meta(), io_stdio(), io_draw());
  let runner = Runner::<IO>::new(&mut heap, &mut host, extrinsics, table, &nets);

  // TODO: `workers` shouldn't be a property in `runtime`?
  // since it's specific to std runtimes.
  let breadth_first = false;
  let workers = 0;

  let (mut stats, flags) = runner.normalize(breadth_first, workers, NoopHooks);
  ensure!(flags.success(), "{}", flags.error_message(false));
  Ok(())
}

#[derive(Default)]
struct IO;

impl<'ivm> ExtTyRegister<'ivm> for IO {
  type With<'x> = IO;
}

impl<'ivm> ExtTyCastStatic<'ivm> for IO {
  const COPY: bool = false;

  fn into_payload_static(_: IO) -> Word {
    Word::from_bits(0)
  }

  unsafe fn from_payload_static(_: Word) -> Self {
    IO
  }
}

fn io_meta<'ivm>() -> impl Register<Host<'ivm>> {
  (
    ExtFn("vi:io:split", |IO| (IO, IO)),
    ExtFn("vi:io:merge", |(IO, IO)| IO),
    ExtFn("vi:io:ready", |IO| (true, IO)),
  )
}

fn io_stdio<'ivm>() -> impl Register<Host<'ivm>> {
  (
    ExtFn("root:io:print_char", move |(IO, n): (IO, u32)| -> IO {
      let char = char::try_from(n).unwrap();
      uefi::system::with_stdout(|stdout| {
        stdout.write_char(char).unwrap();
      });
      IO
    }),
    ExtFn("root:io:print_byte", move |(IO, b): (IO, u32)| -> IO {
      if let Ok(char) = char::try_from(b) {
        // todo not good to silently drop these :(
        uefi::system::with_stdout(|stdout| {
          stdout.write_char(char).unwrap();
        });
      }
      IO
    }),
    ExtFn("root:io:flush", move |IO| -> IO {
      // i think UEFI just flushes for us?
      IO
    }),
    ExtFn("physics:io:clear_display", move |IO| -> IO {
      DISPLAY.0.borrow_mut().as_mut().unwrap().clear(Rgb888::BLACK);
      IO
    }),
  )
}

const SCALE: f32 = 8.0;
const STROKE: u32 = 2;

fn io_draw<'ivm>() -> impl Register<Host<'ivm>> {
  (
    ExtFn(
      "physics:io:draw_circle",
      move |(IO, center_x, center_y, radius): (IO, f32, f32, f32)| -> IO {
        let mut display = DISPLAY.0.borrow_mut();
        let display = display.as_mut().unwrap();

        let top_left_x = (center_x - radius) * SCALE;
        let top_left_y = (-center_y - radius) * SCALE;
        let diameter = radius * 2.0 * SCALE;

        Circle::new(
          Point::new(
            (top_left_x + display.width() as f32 / 2.0) as i32,
            (top_left_y + display.height() as f32 / 2.0) as i32,
          ),
          diameter as u32,
        )
        .into_styled(PrimitiveStyle::with_stroke(Rgb888::WHITE, STROKE))
        .draw(display);
        IO
      },
    ),
    ExtFn(
      "physics:io:draw_rectangle",
      move |(IO, center_x, center_y, half_size_x, half_size_y, cos, sin): (
        IO,
        f32,
        f32,
        f32,
        f32,
        f32,
        f32,
      )|
            -> IO {
        draw_rect(
          center_x * SCALE,
          center_y * SCALE,
          half_size_x * SCALE,
          half_size_y * SCALE,
          cos,
          sin,
        );
        IO
      },
    ),
    // no `read_byte`, `read_char` fns :(
  )
}

fn draw_rect(cx: f32, cy: f32, hx: f32, hy: f32, cos: f32, sin: f32) {
  let mut display = DISPLAY.0.borrow_mut();
  let display = display.as_mut().unwrap();
  let screen_cx = display.width() as f32 / 2.0;
  let screen_cy = display.height() as f32 / 2.0;

  let corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)].map(|(x, y)| {
    let world_x = cx + x * cos - y * sin;
    let world_y = cy + x * sin + y * cos;
    Point::new((screen_cx + world_x) as i32, (screen_cy - world_y) as i32)
  });

  let style = PrimitiveStyle::with_stroke(Rgb888::WHITE, STROKE);
  for i in 0..corners.len() {
    Line::new(corners[i], corners[(i + 1) % corners.len()])
      .into_styled(style)
      .draw(display)
      .unwrap();
  }
}
