# Vine for Zed

Provides `.vi` file recognition, syntax highlighting with Vine's Tree-sitter
parser, and the language server supplied by `vine lsp`.

## File icon

Select **Zed + Vine** in Zed's icon theme selector to use the grayscale VI
branding icon for `.vi` files. All other file and folder icons fall back to
Zed's defaults. The icon is derived from `docs/img/logo.svg`, with a tighter
view box for legibility at file-icon sizes.

## Install

This checkout is installed locally as a prebuilt dev extension, with generated
Wasm artifacts ignored by Git. To reproduce the build:

1. Build the Vine CLI from the repository root: `cargo build --bin vine`.
2. Generate the parser in `lsp/tree-sitter-vine` using Tree-sitter CLI 0.25.10:
   `tree-sitter generate`. Node.js is required to evaluate `grammar.js`.
3. Install the Rust target: `rustup target add wasm32-wasip2`.

Build this crate with
`cargo build --target wasm32-wasip2`, copy
`target/wasm32-wasip2/debug/vine_zed.wasm` to `extension.wasm`, and compile the
parser using wasi-sdk:

```sh
clang -fPIC -shared -Os -Wl,--export=tree_sitter_vine \
  -I ../tree-sitter-vine/src ../tree-sitter-vine/src/parser.c \
  -o grammars/vine.wasm
```

Here `clang` must be the wasi-sdk compiler, not the host compiler. Link this
complete directory into Zed's `extensions/installed/vine` directory to load it
as a dev extension. Do not replace an existing extension installation.

Zed's **zed: install dev extension** action can also build extensions, but its
grammar builder checks out the manifest's pinned repository revision. This
repository does not track generated `src/parser.c`, so that action requires a
separate grammar Git repository with generated sources committed; set
`grammars.vine.repository` and `rev` accordingly. The prebuilt installation
above uses the existing local grammar and does not need that extra repository.

## Language server configuration

By default the extension runs `vine lsp`, resolving `vine` on the worktree's
PATH. Override the executable, the complete argument list, and optional
environment variables in Zed's project or user settings:

```json
{
  "lsp": {
    "vine": {
      "binary": {
        "path": "/absolute/path/to/vine/target/debug/vine",
        "arguments": ["lsp", "--no-root", "root/", "src/main.vi"]
      }
    }
  }
}
```

Entrypoints and library options are CLI arguments, not LSP initialization
options. Glob arguments are expanded by Vine itself, relative to the worktree.
When using the installed standard library, omit `--no-root` and `root/`.
The repository's `.zed/settings.json` uses its existing debug binary and the
same entrypoints as `.vscode/settings.json`.

The extension does not download or build the Vine executable automatically.
Rebuild the CLI after making compiler or LSP changes, and restart the language
server in Zed. Rebuild `extension.wasm` after changing extension Rust code and
run **zed: reload extensions**.
