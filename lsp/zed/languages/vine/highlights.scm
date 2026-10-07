
[
  "pub"
  "mod"
  "use"
  "as"
  "fn"
  "struct"
  "enum"
  "type"
  "trait"
  "impl"
  "match"
  "let"
  "assert"
  "const"
  "in"
  "is"
  "return"
  "do"
  "loop"
  "while"
  "for"
  "break"
  "continue"
  "if"
  "when"
  "else"
  "true"
  "false"
  "and"
  "or"
  "try"
  "unsafe"
  "safe"
] @keyword

[
  "."
  "..."
  ","
  ";"
  ":"
  "::"
  "#"
  "#["
  "["
  "]"
  "("
  ")"
  "{"
  "}"
  "_"
] @punctuation

[
  "&"
  "|"
  "^"
  "+"
  "++"
  "-"
  "*"
  "**"
  "/"
  "%"
  "!"
  "?"
  "~"
  "="
  "=="
  "!="
  "<"
  ">"
  "<="
  ">="
  "->"
  "<<"
  ">>"
  ".."
  "..="
] @operator

(string ["\"" (string_content)] @string)
(char) @string
(num) @number
(string_escape) @string.escape

(line_comment) @comment
(block_comment) @comment

(chain_method (ident) @function)
(expr_path (path (ident) @function .) (exprs))
(expr_path (path (ident) @function . (generic_args) .) (exprs))

(path (ident) @type (ident))
(use_tree (ident) @type (use_tree))
(ty_path (path (ident) @type .))
(ty_path (path (ident) @type . (generic_args) .))
(ty_param (ident) @type)
(item_fn (ident) @function)
(stmt_let_fn (ident) @function)
