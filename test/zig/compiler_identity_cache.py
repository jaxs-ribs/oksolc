#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

"""Exercise Zig's configure cache against the real compiler input manifest."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


BUILD = r'''
const std = @import("std");
const Identity = @import("build_support/compiler_identity.zig");

pub fn build(b: *std.Build) !void {
    const io = b.graph.io;
    var root = try b.root.openDir(io, ".", .{});
    defer root.close(io);
    var inputs = try Identity.collectInputs(b.allocator, io, root);
    defer inputs.deinit(b.allocator);
    inputs.trackConfiguration(b);
    const identity = try Identity.digestInputs(b.allocator, io, root, inputs);

    // This fixture-only counter proves that cache hits skip configuration.
    const old = root.readFileAlloc(io, "configure-count", b.allocator, .limited(64)) catch |err| switch (err) {
        error.FileNotFound => "0",
        else => return err,
    };
    const count = try std.fmt.parseInt(usize, old, 10);
    try root.writeFile(io, .{
        .sub_path = "configure-count",
        .data = try b.allocator.print("{d}", .{count + 1}),
    });
    const output = b.addWriteFiles().add("identity", &identity);
    b.getInstallStep().dependOn(&b.addInstallFile(output, "identity").step);
}
'''


def main():
    source = Path(__file__).resolve().parents[2]
    zig = sys.argv[1] if len(sys.argv) > 1 else "zig"
    with tempfile.TemporaryDirectory(prefix="oksolc-configure-cache-") as temporary:
        root = Path(temporary)
        for directory in ("src/nested", "src/cli/browser", "include", "vendor/sqlite", "build_support"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "build_support/compiler_identity.zig", root / "build_support/compiler_identity.zig")
        (root / "build.zig").write_text(BUILD)
        (root / "build.zig.zon").write_text(
            '.{ .name = .solidity_zig, .version = "0.0.0", .fingerprint = 0x532998ed66b1991d, '
            '.paths = .{ "build.zig", "build.zig.zon", "build_support", "src", "include", "vendor" } }\n'
        )
        (root / "src/nested/input.zig").write_text("first")
        (root / "src/cli/browser/app.ts").write_text("first")

        def build():
            result = subprocess.run(
                [zig, "build", "--cache-poison=disallowed", f"-j{os.cpu_count() or 1}", "--summary", "failures"],
                cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if result.returncode:
                raise RuntimeError(result.stdout)
            return (root / "zig-out/identity").read_text(), int((root / "configure-count").read_text())

        original, count = build()
        assert count == 1
        assert build() == (original, count), "unchanged inputs must reuse configuration"

        (root / "src/cli/browser/app.ts").write_text("changed UI")
        (root / "README.md").write_text("changed documentation")
        assert build() == (original, count), "UI and documentation must not invalidate compiler configuration"

        (root / "src/nested/input.zig").write_text("other")
        edited, count = build()
        assert count == 2 and edited != original, "source bytes must invalidate cached identity"
        os.utime(root / "src/nested/input.zig", None)
        assert build() == (edited, count), "mtime-only edits must preserve content-based configuration"

        (root / "src/nested/new.zig").write_text("new input")
        added, count = build()
        assert count == 3 and added != edited, "new files must invalidate cached identity"
        (root / "src/nested/new.zig").rename(root / "src/nested/renamed.zig")
        renamed, count = build()
        assert count == 4 and renamed != added, "renamed files must invalidate cached identity"
        (root / "src/nested/renamed.zig").unlink()
        assert build() == (edited, 5), "removed files must restore the prior content identity"

        (root / "src/new-directory").mkdir()
        (root / "src/new-directory/child.zig").write_text("first")
        nested, count = build()
        assert count == 6 and nested != edited
        (root / "src/new-directory/child.zig").write_text("other")
        changed, count = build()
        assert count == 7 and changed != nested, "new directories' children must become tracked inputs"
    print("Compiler configure cache: cache hits, edits, additions, renames, deletions, and excluded assets passed")


if __name__ == "__main__":
    main()
