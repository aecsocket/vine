use zed_extension_api::{self as zed, settings::LspSettings, Command, LanguageServerId, Worktree};

struct VineExtension;

impl zed::Extension for VineExtension {
  fn new() -> Self {
    Self
  }

  fn language_server_command(
    &mut self,
    language_server_id: &LanguageServerId,
    worktree: &Worktree,
  ) -> zed::Result<Command> {
    let settings = LspSettings::for_worktree(language_server_id.as_ref(), worktree)?;
    let binary = settings.binary.as_ref();
    let command = binary
            .and_then(|binary| binary.path.clone())
            .or_else(|| worktree.which("vine"))
            .ok_or_else(|| {
                "Vine was not found on PATH. Build it with `cargo build --bin vine` and set lsp.vine.binary.path in Zed settings.".to_string()
            })?;
    let args =
      binary.and_then(|binary| binary.arguments.clone()).unwrap_or_else(|| vec!["lsp".into()]);
    let mut env = worktree.shell_env();
    if let Some(overrides) = binary.and_then(|binary| binary.env.as_ref()) {
      for (key, value) in overrides {
        env.retain(|(existing, _)| existing != key);
        env.push((key.clone(), value.clone()));
      }
    }
    Ok(Command { command, args, env })
  }
}

zed::register_extension!(VineExtension);
