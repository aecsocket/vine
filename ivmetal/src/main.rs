#![no_std]
#![no_main]

use core::{fmt::Write, time::Duration};

use anyhow::{Context, Result, ensure};
use ivm::{
  host::{
    Host, IVM,
    ext::{ExtFn, ExtTyRegister, common},
    runner::Runner,
  },
  runtime::{NoopHooks, ext::ExtTyCastStatic, heap::Heap, word::Word},
};
use log::{error, info};
use uefi::prelude::*;
use vine_util::register::Register;

#[entry]
fn main() -> Status {
  uefi::helpers::init().unwrap();
  info!("hello world!");
  boot::stall(Duration::from_secs(1));
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
  // setup copied from `ivm_cli.rs`
  let src_contents = include_str!("hi.iv");
  let table = &mut ivy::name::Table::default();
  let nets = ivy::text::parser::Parser::parse(table, &src_contents).context("parsing program")?;
  let nets = nets.to_flat_nets().context("creating flat nets")?;

  // setup copied from `RunArgs::run`
  let mut heap = Heap::new();
  let mut ivm = IVM::new();
  let mut host = Host::new(&mut ivm);
  let extrinsics = (common::all(), io_stdio());
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
    // no `read_byte`, `read_char` fns :(
  )
}
