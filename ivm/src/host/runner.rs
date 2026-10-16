use hashbrown::HashMap;

use ivy::{
  name::{NameId, Table},
  net::FlatNet,
};
use vine_util::register::Register;

use crate::{
  host::{Host, ext},
  program::Program,
  runtime::{
    Hooks, Runtime,
    ext::{ExtTy, ExtTyCastStatic},
    flags::Flags,
    heap::Heap,
    port::{Port, Tag},
    stats::Stats,
    wire::Wire,
  },
};

#[cfg(feature = "std")]
pub struct Runner<'ivm, 'ext, IO = ext::platform::IO> {
  io: ExtTy<'ivm, IO>,
  runtime: Runtime<'ivm, 'ext>,
  root: Wire<'ivm>,
}

#[cfg(not(feature = "std"))]
pub struct Runner<'ivm, 'ext, IO> {
  io: ExtTy<'ivm, IO>,
  runtime: Runtime<'ivm, 'ext>,
  root: Wire<'ivm>,
}

impl<'ivm, 'ext, IO> Runner<'ivm, 'ext, IO>
where
  IO: ext::ExtTyRegister<'ivm> + ExtTyCastStatic<'ivm> + Default,
{
  pub fn new(
    heap: &'ivm mut Heap,
    host: &'ext mut Host<'ivm>,
    extrinsics: impl Register<Host<'ivm>>,
    table: &mut Table,
    nets: &HashMap<NameId, FlatNet>,
  ) -> Self {
    Self::with_io(heap, host, extrinsics, table, nets, IO::default())
  }
}

impl<'ivm, 'ext, IO> Runner<'ivm, 'ext, IO>
where
  IO: ext::ExtTyRegister<'ivm> + ExtTyCastStatic<'ivm>,
{
  pub fn with_io(
    heap: &'ivm mut Heap,
    host: &'ext mut Host<'ivm>,
    extrinsics: impl Register<Host<'ivm>>,
    table: &mut Table,
    nets: &HashMap<NameId, FlatNet>,
    io: IO,
  ) -> Self {
    let io_ty = host.register_ext_ty::<IO>();

    host.register(table, extrinsics);

    let program = Program::new(host, table, nets);
    let main = table.add_path_name("iv:main");
    let main = program.graft(main).expect("missing main");

    let mut runtime = host.init(heap);

    let node = unsafe { runtime.new_node(Tag::Comb, 0) };
    runtime.link_wire(node.1, Port::new_ext_val(io_ty.wrap_static(io)));
    runtime.link(Port::new_graft(main), node.0);

    Self { io: io_ty, root: node.2, runtime }
  }

  pub fn normalize(
    mut self,
    breadth_first: bool,
    workers: usize,
    hooks: impl Hooks,
  ) -> (Stats, Flags) {
    if breadth_first {
      self.runtime.normalize_breadth_first(hooks);
    } else {
      #[cfg(feature = "std")]
      {
        if workers > 0 {
          self.runtime.normalize_parallel(workers)
        } else {
          self.runtime.normalize(hooks);
        }
      }
      #[cfg(not(feature = "std"))]
      {
        _ = workers;
        self.runtime.normalize(hooks);
      }
    }

    let out = self.runtime.follow(Port::new_wire(self.root));

    self.runtime.flags.no_io =
      out.tag() != Tag::ExtVal || unsafe { out.as_ext_val() }.ty_id() != self.io.id();
    self.runtime.flags.vicious = self.runtime.stats.mem_free < self.runtime.stats.mem_alloc;

    (self.runtime.stats, self.runtime.flags)
  }
}
