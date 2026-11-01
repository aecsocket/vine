//! Encode Ivy nets as immutable, page-backed hardware grafts.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail, ensure};
use ivy::{
  name::{NameId, Table},
  net::{FlatNet, FlatNode, Wire},
  text::{
    lexer::{Lexer, Token},
    parser::Parser,
  },
};

pub mod abi;

#[derive(Debug, Clone)]
pub struct ProgramImage {
  pub words: Vec<u64>,
  pub entry_addr: usize,
  pub grafts: BTreeMap<String, usize>,
}

enum Reference {
  Graft(NameId),
  Branch(Vec<NameId>),
}

enum Word {
  Packed(u64),
  Reference(Reference),
}

struct Relocation {
  address: usize,
  source: NameId,
  reference: Reference,
}

/// Parse Ivy source and encode all its definitions, including unreachable
/// grafts.
///
/// `entry` is an Ivy name, including any payload and parameters. Graft map keys
/// use Ivy's canonical name spelling. Reserve every image page before
/// execution.
pub fn assemble(source: &str, entry: &str) -> Result<ProgramImage> {
  let mut table = Table::default();
  let parsed = Parser::parse(&mut table, source).context("parsing Ivy source")?;
  let mut names = BTreeSet::new();
  // Flattening collects into a map, so duplicates must be checked first.
  for (id, _) in &parsed.nets {
    ensure!(names.insert(*id), "duplicate net name `{}`", table.show_name_id(*id));
  }
  let nets = parsed.to_flat_nets().context("flattening Ivy nets")?;

  let entry_id = {
    let mut parser = Parser::new(&mut table, Lexer::new(entry)).context("parsing entry name")?;
    let id = parser.parse_name_id().context("parsing entry name")?;
    ensure!(parser.state.token == Token::Eof, "unexpected trailing input in entry name `{entry}`");
    id
  };
  ensure!(names.contains(&entry_id), "entry net `{entry}` is not defined");

  let mut nets: Vec<_> = nets.into_iter().collect();
  nets.sort_by_cached_key(|(id, _)| table.show_name_id(*id).to_string());

  let mut words = Vec::new();
  let mut grafts = BTreeMap::new();
  let mut relocations = Vec::new();
  for (id, net) in nets {
    let (header, instructions) = encode_net(&table, &net)
      .with_context(|| format!("encoding graft `{}`", table.show_name_id(id)))?;
    let address = emit(&mut words, header, true)?;
    grafts.insert(id, address);
    for instruction in instructions {
      match instruction {
        Word::Packed(word) => {
          emit(&mut words, word, true)?;
        }
        Word::Reference(reference) => {
          let address = emit(&mut words, 0, true)?;
          relocations.push(Relocation { address, source: id, reference });
        }
      }
    }
  }

  // Unlike streams, branch tables are raw contiguous words, including footers.
  // Allocate them in first-reference order, not map-key order.
  align_page(&mut words)?;
  let mut tables = BTreeMap::<Vec<NameId>, usize>::new();
  for relocation in &relocations {
    if let Reference::Branch(targets) = &relocation.reference {
      if tables.contains_key(targets) {
        continue;
      }
      tables.insert(targets.clone(), words.len());
      emit(&mut words, targets.len() as u64, false)?;
      for target in targets {
        let address = grafts.get(target).with_context(|| {
          format!(
            "undefined graft `{}` in branch table referenced by `{}`",
            table.show_name_id(*target),
            table.show_name_id(relocation.source),
          )
        })?;
        emit(&mut words, abi::port(abi::TAG_GRAFT, 0, *address as u64), false)?;
      }
    }
  }
  align_page(&mut words)?;

  for relocation in relocations {
    let (tag, label, target) = match relocation.reference {
      Reference::Graft(target) => {
        let address = grafts.get(&target).with_context(|| {
          format!(
            "undefined graft `{}` referenced by `{}`",
            table.show_name_id(target),
            table.show_name_id(relocation.source),
          )
        })?;
        (abi::TAG_GRAFT, 0, *address)
      }
      Reference::Branch(targets) => (abi::TAG_EXT_VAL, type_label("ivm:branch")?, tables[&targets]),
    };
    words[relocation.address] = abi::port(tag, label, target as u64);
  }

  Ok(ProgramImage {
    entry_addr: grafts[&entry_id],
    grafts: grafts
      .into_iter()
      .map(|(id, addr)| (table.show_name_id(id).to_string(), addr))
      .collect(),
    words,
  })
}

fn encode_net(table: &Table, net: &FlatNet) -> Result<(u64, Vec<Word>)> {
  ensure!(
    net.free.len() == 1,
    "a graft must have exactly one free/root port; found {}",
    net.free.len()
  );
  let canonical = |wire: Wire| net.links.canonicalize_ref(wire);
  let root = canonical(net.free[0]);
  let mut endpoints = BTreeMap::<Wire, usize>::new();
  for wire in net.free.iter().copied().chain(
    net.nodes.iter().flat_map(|node| std::iter::once(node.pri).chain(node.aux.iter().copied())),
  ) {
    *endpoints.entry(canonical(wire)).or_default() += 1;
  }
  for (wire, count) in &endpoints {
    ensure!(
      *count == 2,
      "wire {} has {count} endpoints; every wire must join exactly two endpoints",
      wire.0,
    );
  }
  ensure!(
    endpoints.len() <= abi::MAX_REGISTERS,
    "needs {} registers; maximum is {}",
    endpoints.len(),
    abi::MAX_REGISTERS,
  );
  let mut registers: BTreeMap<_, _> = endpoints
    .keys()
    .copied()
    .filter(|&wire| wire != root)
    .enumerate()
    .map(|(index, wire)| (wire, index + 1))
    .collect();
  registers.insert(root, 0);
  ensure!(
    u32::try_from(net.nodes.len()).is_ok(),
    "instruction count exceeds the 32-bit graft header"
  );

  let mut words = Vec::new();
  let mut error = false;
  // FlatNet order is already the frontend's order; do not sort nodes.
  for node in &net.nodes {
    let name = &node.name;
    let path = table.path(name.path);
    let pri = registers[&canonical(node.pri)];
    match path {
      "ivm:x" | "ivm:y" | "ivm:ext:merge" | "ivm:ext:merge:swap" => {
        let merge = matches!(path, "ivm:ext:merge" | "ivm:ext:merge:swap");
        require(table, node, 2, Some(usize::from(merge)), false)?;
        let (tag, label) = if merge {
          let function = table.name(name.children[0]);
          let index = abi::EXT_FNS
            .iter()
            .position(|&path| path == table.path(function.path))
            .filter(|_| function.is_path())
            .with_context(|| {
              format!("unknown extrinsic function `{}`", table.show_name(function))
            })?;
          // Every function in this hardware ABI takes two inputs.
          let swap = if path.ends_with(":swap") { abi::SWAP_BIT } else { 0 };
          (abi::TAG_EXT_FN, index as u16 | swap)
        } else {
          (abi::TAG_COMB, u16::from(path == "ivm:y"))
        };
        words.push(Word::Packed(abi::binary(
          tag,
          label,
          [pri, registers[&canonical(node.aux[0])], registers[&canonical(node.aux[1])]],
        )));
        continue;
      }
      _ => {}
    }

    let value = match path {
      "ivm:eraser" | "ivm:error" => {
        require(table, node, 0, Some(0), false)?;
        error |= path == "ivm:error";
        Word::Packed(abi::port(abi::TAG_ERASE, 0, 0))
      }
      "ivm:graft" => {
        require(table, node, 0, Some(1), false)?;
        Word::Reference(Reference::Graft(name.children[0]))
      }
      "ivm:n32" => {
        require(table, node, 0, Some(0), true)?;
        let payload = name.payload.as_u32().with_context(|| {
          format!("N32 literal does not fit in 32 bits: `{}`", table.show_name(name))
        })?;
        Word::Packed(abi::port(abi::TAG_EXT_VAL, type_label(path)?, u64::from(payload)))
      }
      "ivm:branch" => {
        require(table, node, 0, None, false)?;
        Word::Reference(Reference::Branch(name.children.clone()))
      }
      _ => bail!("unsupported hardware node `{}`", table.show_name(name)),
    };
    words.push(Word::Packed(abi::nilary(pri)));
    words.push(value);
  }

  Ok((abi::graft(registers.len(), error, net.nodes.len()), words))
}

fn require(
  table: &Table,
  node: &FlatNode,
  arity: usize,
  parameters: Option<usize>,
  has_payload: bool,
) -> Result<()> {
  ensure!(
    node.aux.len() == arity,
    "node `{}` needs {arity} auxiliary ports; found {}",
    table.show_name(&node.name),
    node.aux.len(),
  );
  if let Some(parameters) = parameters {
    ensure!(
      node.name.children.len() == parameters,
      "node `{}` needs {parameters} name parameters; found {}",
      table.show_name(&node.name),
      node.name.children.len(),
    );
  }
  ensure!(
    has_payload || node.name.payload.is_zero(),
    "unexpected payload on `{}`",
    table.show_name(&node.name),
  );
  Ok(())
}

fn type_label(path: &str) -> Result<u16> {
  abi::EXT_TYS
    .iter()
    .position(|&name| name == path)
    .map(|index| index as u16)
    .with_context(|| format!("hardware ABI is missing extrinsic type `{path}`"))
}

fn emit(words: &mut Vec<u64>, word: u64, stream: bool) -> Result<usize> {
  ensure!(words.len() < abi::CAPACITY, "program image exceeds the {}-word heap", abi::CAPACITY);
  if stream && words.len() % abi::PAGE_SIZE == abi::PAGE_SIZE - 1 {
    words.push(((words.len() + 1) / abi::PAGE_SIZE) as u64);
  }
  ensure!(words.len() < abi::CAPACITY, "program image exceeds the {}-word heap", abi::CAPACITY);
  let address = words.len();
  words.push(word);
  Ok(address)
}

fn align_page(words: &mut Vec<u64>) -> Result<()> {
  let padding = (abi::PAGE_SIZE - words.len() % abi::PAGE_SIZE) % abi::PAGE_SIZE;
  ensure!(
    words.len() + padding <= abi::CAPACITY,
    "page-padded program image exceeds the {}-word heap",
    abi::CAPACITY,
  );
  words.resize(words.len() + padding, 0);
  Ok(())
}
