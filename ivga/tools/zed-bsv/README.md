# Bluespec SystemVerilog + Blues for Zed

Local development extension based on [sandytruant/zed-bsv](https://github.com/sandytruant/zed-bsv)
(commit `63d40b87e34744426bb92012043eb5df3785fa18`). The upstream Apache-2.0
license and syntax queries are preserved. This version adds a launcher for the
[Blues language server](https://gitlab.com/runtimetantrum/blues-lsp).

## Install

1. Install the server: `cargo install blues-lsp --version 0.1.4 --locked`.
2. In Zed, run **zed: install dev extension** and select this directory
   (`ivga/tools/zed-bsv`). Zed builds the WASM extension and downloads/builds its
   pinned Tree-sitter grammar. Rust/rustup must be available to Zed.
3. Reopen a `.bsv` file, or run **editor: restart language server**.

This uses the same extension ID as the published Bluespec SystemVerilog
extension, so installing it as a dev extension overrides the published version.
It launches `blues lsp` from the worktree's PATH. It does not download or update
server binaries automatically.

If Zed cannot find the server, configure an absolute path in Zed settings:

```json
{
  "lsp": {
    "blues": {
      "binary": {
        "path": "/absolute/path/to/blues",
        "arguments": ["lsp"]
      }
    }
  }
}
```

The language server ID is `blues`; the language name is `Bluespec SystemVerilog`.
If you explicitly list language servers in your settings, include `blues` there.
Use **zed: open log** to troubleshoot launch failures.

## Project configuration

Blues 0.1.4 reads `blues_compdb.json` from its workspace root. Compile databases
are provided at both the Vine root and `ivga/`, so either folder can be opened
in Zed. They describe the same `ivga/src/Top.bsv` build with directory-relative
paths. Keep both updated when changing compiler search paths or top modules.
The compiler entry uses this machine's real BSC installation path rather than
`~/.local/bin/bsc`: Blues infers its standard-library location from that path,
and cannot infer it correctly through our shell launcher. Update both databases
if moving the project to another machine or upgrading BSC.

Blues provides navigation, rename, symbols, and syntax diagnostics. It is still
experimental and does not replace BSC's type checking or scheduling diagnostics.

## Validate the extension

From this directory:

```sh
rustup target add wasm32-wasip2
cargo fmt --check
cargo build --release --target wasm32-wasip2 --locked
```

Zed's dev-extension installer handles the grammar and extension loading; a Cargo
build alone does not install it into the editor.
