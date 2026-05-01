#!/bin/bash
# diffview installer

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/bin"
TARGET="${INSTALL_DIR}/diffview"

echo "=== diffview installer ==="

# Create ~/.local/bin if needed
mkdir -p "$INSTALL_DIR"

# Copy script
cp "${SCRIPT_DIR}/diffview.py" "$TARGET"
chmod +x "$TARGET"

# Ensure shebang is executable directly
if ! head -1 "$TARGET" | grep -q "^#!/usr/bin/env python3"; then
    sed -i '1s|^|#!/usr/bin/env python3\n|' "$TARGET"
fi

echo "✓ Installed to: $TARGET"

# Check PATH
if [[ ":$PATH:" != *":${INSTALL_DIR}:"* ]]; then
    echo ""
    echo "  Add to your shell profile (~/.bashrc or ~/.zshrc):"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Register as git tool
echo ""
read -p "Register as git difftool and mergetool? [Y/n] " ans
ans="${ans:-Y}"
if [[ "$ans" =~ ^[Yy] ]]; then
    python3 "$TARGET" --install-git
fi

echo ""
echo "Done! Try: diffview file1.txt file2.txt"
echo "      Or:  diffview dir1/ dir2/"
echo "      Or:  diffview HEAD~3 HEAD"
