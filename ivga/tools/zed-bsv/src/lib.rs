use zed_extension_api::{self as zed, settings::LspSettings};

struct BsvExtension;

impl zed::Extension for BsvExtension {
  fn new() -> Self {
    Self
  }

  fn language_server_command(
    &mut self,
    language_server_id: &zed::LanguageServerId,
    worktree: &zed::Worktree,
  ) -> zed::Result<zed::Command> {
    let settings = LspSettings::for_worktree(language_server_id.as_ref(), worktree)?;
    let binary = settings.binary;
    let command = binary
            .as_ref()
            .and_then(|binary| binary.path.clone())
            .or_else(|| worktree.which("blues"))
            .ok_or_else(|| {
                "Blues was not found. Run `cargo install blues-lsp --locked`, or set lsp.blues.binary.path in Zed settings."
                    .to_string()
            })?;
    let args =
      binary.and_then(|binary| binary.arguments).unwrap_or_else(|| vec!["lsp".to_string()]);

    Ok(zed::Command { command, args, env: worktree.shell_env() })
  }
}

zed::register_extension!(BsvExtension);
