#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

"""Exercise make-time identities, configure cache hits, and concurrent edits."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


BUILD = r'''
const std = @import("std");
const Identity = @import("build_support/compiler_identity.zig");

pub fn build(b: *std.Build) !void {
    const race = b.option(bool, "race", "Pause configuration for a concurrent source edit") orelse false;
    const io = b.graph.io;
    var root = try b.root.openDir(io, ".", .{});
    defer root.close(io);
    const identity = Identity.create(b, null);
    const module = b.createModule(.{
        .root_source_file = b.path("probe.zig"),
        .target = b.graph.host,
        .optimize = .debug,
    });
    module.addAnonymousImport("identity", .{ .root_source_file = identity.file });
    const probe = b.addExecutable(.{ .name = "identity-probe", .root_module = module });
    const validation = identity.validate(b, probe).?;
    const mutate = b.addSystemCommand(&.{"python3"});
    mutate.addFileArg2(b.path("mutate.py"), .{});
    mutate.setCwd(b.path("."));
    mutate.has_side_effects = true;
    mutate.step.dependOn(&probe.step);
    validation.dependOn(&mutate.step);
    const install = b.addInstallArtifact(probe, .{});
    install.step.dependOn(validation);
    b.getInstallStep().dependOn(&install.step);
    const install_identity = b.addInstallFile(identity.file, "identity");
    install_identity.step.dependOn(validation);
    b.getInstallStep().dependOn(&install_identity.step);

    // Fixture-only observations prove whether the configure process ran.
    const old = root.readFileAlloc(io, "configure-count", b.allocator, .limited(64)) catch |err| switch (err) {
        error.FileNotFound => "0",
        else => return err,
    };
    const count = try std.fmt.parseInt(usize, old, 10);
    try root.writeFile(io, .{
        .sub_path = "configure-count",
        .data = try b.allocator.print("{d}", .{count + 1}),
    });
    if (race) {
        try root.writeFile(io, .{ .sub_path = "configure-ready", .data = "ready" });
        while (true) {
            root.access(io, "configure-resume", .{}) catch |err| switch (err) {
                error.FileNotFound => {
                    try std.Io.sleep(io, .fromMilliseconds(1), .awake);
                    continue;
                },
                else => return err,
            };
            break;
        }
    }
}
'''

PROBE = r'''
const std = @import("std");
pub fn main(init: std.process.Init) !void {
    try std.Io.File.stdout().writeStreamingAll(init.io, @embedFile("identity"));
}
'''

MUTATE = '''from pathlib import Path
marker = Path("mutation")
if marker.exists():
    Path("src/nested/input.zig").write_text(marker.read_text())
    marker.unlink()
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
        (root / "probe.zig").write_text(PROBE)
        (root / "mutate.py").write_text(MUTATE)
        (root / "build.zig.zon").write_text(
            '.{ .name = .solidity_zig, .version = "0.0.0", .fingerprint = 0x532998ed66b1991d, '
            '.paths = .{ "build.zig", "build.zig.zon", "build_support", "src", "include", "vendor", "probe.zig", "mutate.py" } }\n'
        )
        (root / "src/nested/input.zig").write_text("first")
        (root / "src/cli/browser/app.ts").write_text("first")
        command = [zig, "build", "--cache-poison=disallowed", f"-j{min(os.cpu_count() or 1, 6)}", "--summary", "failures"]
        executable = root / "zig-out/bin/identity-probe"

        def installed():
            identity = (root / "zig-out/identity").read_text()
            assert subprocess.check_output([str(executable)], text=True) == identity
            return identity, int((root / "configure-count").read_text()), executable.stat().st_mtime_ns

        def build(extra=(), success=True):
            result = subprocess.run(command + list(extra), cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            if success:
                if result.returncode:
                    raise RuntimeError(result.stdout)
                return installed()
            assert result.returncode and "CompilerInputsChangedDuringBuild" in result.stdout, result.stdout

        original = build()
        assert original[1] == 1
        assert build() == original, "unchanged inputs must reuse configuration and the executable"
        (root / "src/cli/browser/app.ts").write_text("changed UI")
        (root / "README.md").write_text("changed documentation")
        assert build() == original, "UI and documentation must not rebuild the compiler"

        (root / "src/nested/input.zig").write_text("other")
        edited = build()
        assert edited[0] != original[0] and edited[1] == 1, "source edits must refresh identity without configuration"
        os.utime(root / "src/nested/input.zig", None)
        assert build() == edited, "mtime-only edits must reuse the executable"
        (root / "src/nested/new.zig").write_text("new input")
        added = build()
        assert added[0] != edited[0] and added[1] == 1
        (root / "src/nested/new.zig").rename(root / "src/nested/renamed.zig")
        renamed = build()
        assert renamed[0] != added[0] and renamed[1] == 1
        (root / "src/nested/renamed.zig").unlink()
        assert build()[0:2] == edited[0:2], "removal must restore the previous identity"
        (root / "src/new-directory").mkdir()
        (root / "src/new-directory/child.zig").write_text("first")
        nested = build()
        assert nested[0] != edited[0] and nested[1] == 1
        (root / "src/new-directory/child.zig").write_text("other")
        changed = build()
        assert changed[0] != nested[0] and changed[1] == 1

        # Edit after configuration starts, before Zig records its dependencies.
        process = subprocess.Popen(command + ["-Drace=true"], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while not (root / "configure-ready").exists():
                if process.poll() is not None:
                    raise RuntimeError(process.communicate()[0])
                if time.monotonic() >= deadline:
                    raise RuntimeError("configure barrier timed out")
                time.sleep(0.01)
            (root / "src/nested/input.zig").write_text("concurrent configuration edit")
            (root / "configure-resume").write_text("resume")
            output = process.communicate(timeout=90)[0]
            assert process.returncode == 0, output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        raced = installed()
        assert raced[0] != changed[0] and raced[1] == 2
        assert build(["-Drace=true"]) == raced

        # A later edit must fail validation, then recover on an unchanged retry.
        (root / "mutation").write_text("edit after compilation")
        build(["-Drace=true"], success=False)
        assert installed() == raced, "a rejected build must not replace the installed artifact"
        recovered = build(["-Drace=true"])
        assert recovered[0] != raced[0] and recovered[1] == 2, "retry must recover without reconfiguration"
        assert build(["-Drace=true"]) == recovered
    print("Compiler identity: configure reuse, source changes, concurrent configuration edits, validation failure and retry recovery passed")


if __name__ == "__main__":
    main()
