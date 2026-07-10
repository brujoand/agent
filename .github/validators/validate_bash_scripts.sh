#!/usr/bin/env bash

set -e

FAILED=0

function bash_std::check_shebang {
  local file="$1"
  local first_line
  first_line=$(head -n 1 "$file")

  if [[ $first_line != "#!/usr/bin/env bash" ]]; then
    echo "[ERROR] $file: Invalid shebang. Must be '#!/usr/bin/env bash', found: $first_line"
    return 1
  fi
  return 0
}

function bash_std::check_set_e {
  local file="$1"

  if [[ ! -x $file ]]; then
    return 0
  fi

  if ! grep -q "^set -e" "$file"; then
    echo "[ERROR] $file: Missing 'set -e' directive"
    return 1
  fi
  return 0
}

function bash_std::check_no_emojis {
  local file="$1"

  if grep -qP '[\x{1F300}-\x{1F9FF}\x{2600}-\x{26FF}\x{2700}-\x{27BF}]' "$file"; then
    echo "[ERROR] $file: Contains emojis (not allowed in scripts)"
    return 1
  fi
  return 0
}

function bash_std::validate_file {
  local file="$1"

  if [[ ! -f $file ]]; then
    return 0
  fi

  if [[ ! $file =~ \.sh$ ]]; then
    return 0
  fi

  local file_failed=0

  bash_std::check_shebang "$file" || file_failed=1
  bash_std::check_set_e "$file" || file_failed=1
  bash_std::check_no_emojis "$file" || file_failed=1

  if [[ $file_failed -eq 1 ]]; then
    FAILED=1
  fi
}

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 <file1.sh> [file2.sh ...]"
  exit 1
fi

for file in "$@"; do
  bash_std::validate_file "$file"
done

if [[ $FAILED -eq 1 ]]; then
  echo ""
  echo "Bash script validation failed. Please fix the issues above."
  echo ""
  echo "Required standards:"
  echo "  - Shebang: #!/usr/bin/env bash"
  echo "  - Include 'set -e' directive (executable scripts only)"
  echo "  - No emojis in scripts"
  exit 1
fi

exit 0
