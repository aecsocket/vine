use std::{
  collections::BTreeMap,
  fmt::Write as _,
  fs,
  io::ErrorKind,
  path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use clap::Parser;
use ivga_assemble::{abi, assemble};
use serde_json::json;

#[derive(Parser)]
#[command(about = "Assemble Ivy nets into a 64-bit FPGA BRAM image")]
struct Args {
  /// Ivy source file.
  source: PathBuf,
  /// Output hex file: one 64-bit word per line in address order.
  #[arg(short, long)]
  output: PathBuf,
  /// JSON metadata; defaults to the output path with a .json extension.
  #[arg(long)]
  manifest: Option<PathBuf>,
  /// Entry net name, optionally including a payload and name parameters.
  #[arg(long, default_value = "iv:main")]
  entry: String,
}

fn output_identity(path: &Path) -> Result<PathBuf> {
  match path.canonicalize() {
    Ok(path) => Ok(path),
    Err(error) if error.kind() == ErrorKind::NotFound => {
      let parent =
        path.parent().filter(|path| !path.as_os_str().is_empty()).unwrap_or(Path::new("."));
      let filename = path.file_name().context("output path must name a file")?;
      Ok(parent.canonicalize()?.join(filename))
    }
    Err(error) => Err(error.into()),
  }
}

fn main() -> Result<()> {
  let args = Args::parse();
  let manifest = args.manifest.unwrap_or_else(|| args.output.with_extension("json"));
  let source_path = args.source.canonicalize().context("resolving source path")?;
  let output_path = output_identity(&args.output).context("resolving image path")?;
  let manifest_path = output_identity(&manifest).context("resolving manifest path")?;
  ensure!(
    source_path != output_path && source_path != manifest_path && output_path != manifest_path,
    "source, image, and manifest must be different files",
  );

  let source = fs::read_to_string(&source_path)
    .with_context(|| format!("reading {}", args.source.display()))?;
  let image = assemble(&source, &args.entry)?;
  let reserved_pages: Vec<_> = (0..image.words.len() / abi::PAGE_SIZE).collect();
  let type_ids: BTreeMap<_, _> =
    abi::EXT_TYS.iter().enumerate().map(|(i, &name)| (name, i)).collect();
  let function_ids: BTreeMap<_, _> =
    abi::EXT_FNS.iter().enumerate().map(|(i, &name)| (name, i)).collect();
  let metadata = json!({
    "version": 1,
    "word_width": abi::WORD_WIDTH,
    "page_size": abi::PAGE_SIZE,
    "word_count": image.words.len(),
    "entry_addr": image.entry_addr,
    "reserved_pages": reserved_pages,
    "grafts": image.grafts,
    "type_ids": type_ids,
    "function_ids": function_ids,
  });
  let mut hex = String::with_capacity(image.words.len() * 17);
  for word in &image.words {
    writeln!(hex, "{word:016x}")?;
  }
  let manifest_json = serde_json::to_string_pretty(&metadata)? + "\n";
  fs::write(&args.output, hex).with_context(|| format!("writing {}", args.output.display()))?;
  fs::write(&manifest, manifest_json).with_context(|| format!("writing {}", manifest.display()))?;
  println!(
    "{} words, {} reserved pages; entry {} at word {}",
    image.words.len(),
    reserved_pages.len(),
    args.entry,
    image.entry_addr,
  );
  Ok(())
}
