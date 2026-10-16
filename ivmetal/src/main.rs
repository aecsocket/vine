#![no_std]
#![no_main]

use core::time::Duration;

use anyhow::{Context, Result, ensure};
use ivm::{
  host::{Host, IVM, ext::common, runner::Runner},
  runtime::{NoopHooks, heap::Heap},
};
use log::{error, info};
use uefi::prelude::*;

#[entry]
fn main() -> Status {
  uefi::helpers::init().unwrap();
  info!("hello world!");
  boot::stall(Duration::from_secs(1));
  match run() {
    Ok(()) => {
      info!("success");
      Status::SUCCESS
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
  let nets = nets.to_flat_nets().context("creating flat nets");

  // setup copied from `RunArgs::run`
  let heap = Heap::new();
  let mut ivm = IVM::new();
  let mut host = Host::new(&mut ivm);
  let extrinsics = (common::all());
  // let runner = Runner::new(&mut heap, &mut host, extrinsics, table, nets);

  let mut runtime = host.init(&mut heap);
  runtime.normalize(hooks);

  let breadth_first = false;
  let workers = 0;

  let (mut stats, flags) = runner.normalize(breadth_first, workers, NoopHooks);
  ensure!(flags.success(), "{}", flags.error_message(false));
  Ok(())
}
