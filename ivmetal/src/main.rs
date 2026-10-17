#![no_std]
#![no_main]

mod graphics;

use core::{cell::RefCell, fmt::Write, mem};

use anyhow::{Context, Result, ensure};
use embedded_graphics::{
  prelude::{Point, Primitive, Size},
  primitives::{Circle, PrimitiveStyle, Rectangle},
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
  let extrinsics = (common::all(), io_meta(), io_stdio());
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
    ExtFn(
      "physics:io:draw_circle",
      move |(IO, top_left_x, top_left_y, diameter): (IO, f32, f32, f32)| -> IO {
        let mut display = DISPLAY.0.borrow_mut();
        let display = display.as_mut().unwrap();
        let (top_left_x, top_left_y, diameter) =
          (top_left_x as i32, top_left_y as i32, diameter as u32);
        Circle::new(Point::new(top_left_x, top_left_y), diameter)
          .into_styled(PrimitiveStyle::with_stroke(Rgb888::WHITE, 1))
          .draw(display);
        IO
      },
    ),
    ExtFn(
      "physics:io:draw_rectangle",
      move |(IO, top_left_x, top_left_y, size_x, size_y): (IO, f32, f32, f32, f32)| -> IO {
        let mut display = DISPLAY.0.borrow_mut();
        let display = display.as_mut().unwrap();
        let (top_left_x, top_left_y, size_x, size_y) =
          (top_left_x as i32, top_left_y as i32, size_x as u32, size_y as u32);
        Rectangle::new(Point::new(top_left_x, top_left_y), Size::new(size_x, size_y))
          .into_styled(PrimitiveStyle::with_stroke(Rgb888::WHITE, 1))
          .draw(display);
        IO
      },
    ),
    // no `read_byte`, `read_char` fns :(
  )
}
