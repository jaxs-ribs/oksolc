#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

"""Check compiled source/header values, configure reuse, and concurrent edits."""

import json
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
const CompilerHeader = @import("build_support/compiler_header.zig");

pub fn build(b: *std.Build) !void {
    const race = b.option(bool, "race", "Pause configuration for a concurrent source edit") orelse false;
    const io = b.graph.io;
    var root = try b.root.openDir(io, ".", .{});
    defer root.close(io);
    const identity = Identity.create(b, null);
    const translate_c = b.dependency("translate_c", .{ .target = b.graph.host, .optimize = .fast });
    const translated = CompilerHeader.translate(translate_c, identity.file, .{
        .c_source_file = b.path("include/revision.h"),
        .target = b.graph.host,
        .optimize = .debug,
    });
    const mutate_header = b.addSystemCommand(&.{"python3"});
    mutate_header.addFileArg2(b.path("mutate.py"), .{});
    mutate_header.addArgs(&.{ "header-mutation", "include/revision.h" });
    mutate_header.setCwd(b.path("."));
    mutate_header.has_side_effects = true;
    mutate_header.step.dependOn(&translated.run.step);
    // Force the reported bad schedule if the shared production helper loses
    // its ordering edge: translate A, edit to B, then capture B's identity.
    // With the edge present, hashing precedes translation and detects the edit.
    const snapshot = b.graph.generated_files.items[@backingInt(identity.file.generated.index)];
    if (!dependsOn(&translated.run.step, snapshot))
        for (snapshot.dependencies.items) |step| step.dependOn(&mutate_header.step);
    const module = b.createModule(.{
        .root_source_file = b.path("probe.zig"),
        .target = b.graph.host,
        .optimize = .debug,
        .imports = &.{.{ .name = "c", .module = translated.mod }},
    });
    module.addAnonymousImport("identity", .{ .root_source_file = identity.file });
    const probe = b.addExecutable(.{ .name = "identity-probe", .root_module = module });
    probe.step.dependOn(&mutate_header.step);
    const validation = identity.validate(b, probe).?;
    const mutate = b.addSystemCommand(&.{"python3"});
    mutate.addFileArg2(b.path("mutate.py"), .{});
    mutate.addArgs(&.{ "mutation", "src/nested/input.zig" });
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

fn dependsOn(step: *const std.Build.Step, required: *const std.Build.Step) bool {
    if (step == required) return true;
    for (step.dependencies.items) |dependency|
        if (dependsOn(dependency, required)) return true;
    return false;
}
'''

PROBE = r'''
const std = @import("std");
pub fn main(init: std.process.Init) !void {
    const output = try init.gpa.print("{s}\n{s}\n{d}\n", .{
        @embedFile("identity"), @import("src/nested/input.zig").value, @import("c").REVISION,
    });
    defer init.gpa.free(output);
    try std.Io.File.stdout().writeStreamingAll(init.io, output);
}
'''

MUTATE = '''from pathlib import Path
import sys
marker = Path(sys.argv[1])
if marker.exists():
    Path(sys.argv[2]).write_text(marker.read_text())
    marker.unlink()
'''


def input_text(value):
    return "pub const value = " + json.dumps(value) + ";\n"


def main():
    source = Path(__file__).resolve().parents[2]
    zig = sys.argv[1] if len(sys.argv) > 1 else "zig"
    packages = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else source / "zig-pkg"
    translate_c = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else next(packages.glob("translate_c-*"))
    translate_c_hash = sys.argv[4] if len(sys.argv) > 4 else translate_c.name
    with tempfile.TemporaryDirectory(prefix="oksolc-configure-cache-") as temporary:
        root = Path(temporary)
        for directory in ("src/nested", "src/cli/browser", "include", "vendor/sqlite", "build_support"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "build_support/compiler_identity.zig", root / "build_support/compiler_identity.zig")
        shutil.copyfile(source / "build_support/compiler_header.zig", root / "build_support/compiler_header.zig")
        (root / "build.zig").write_text(BUILD)
        (root / "probe.zig").write_text(PROBE)
        (root / "mutate.py").write_text(MUTATE)
        (root / "build.zig.zon").write_text(
            '.{ .name = .solidity_zig, .version = "0.0.0", .fingerprint = 0x532998ed66b1991d, '
            '.dependencies = .{ .translate_c = .{ .url = "https://example.invalid/translate_c.tar.gz", .hash = '
            + json.dumps(translate_c_hash) + ' } }, '
            '.paths = .{ "build.zig", "build.zig.zon", "build_support", "src", "include", "vendor", "probe.zig", "mutate.py" } }\n'
        )
        (root / "src/nested/input.zig").write_text(input_text("first"))
        (root / "include/revision.h").write_text("#define REVISION 1\n")
        (root / "src/cli/browser/app.ts").write_text("first")
        command = [zig, "build", "--system", str(packages), "--fork", str(translate_c), "--cache-poison=disallowed", f"-j{min(os.cpu_count() or 1, 6)}", "--summary", "failures"]
        executable = root / "zig-out/bin/identity-probe"

        def installed():
            identity = (root / "zig-out/identity").read_text()
            embedded, value, revision = subprocess.check_output([str(executable)], text=True).splitlines()
            assert embedded == identity
            return identity, int((root / "configure-count").read_text()), executable.stat().st_mtime_ns, value, int(revision)

        def build(extra=(), success=True):
            result = subprocess.run(command + list(extra), cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            if success:
                if result.returncode:
                    raise RuntimeError(result.stdout)
                result = installed()
                expected = json.loads((root / "src/nested/input.zig").read_text().split(" = ", 1)[1].removesuffix(";\n"))
                assert result[3] == expected, "compiled source value must match the identified source revision"
                assert result[4] == int((root / "include/revision.h").read_text().split()[-1]), "translated header value must match the identified header revision"
                return result
            assert result.returncode, "build accepted a concurrent compiler input edit: " + repr(installed())
            assert "CompilerInputsChangedDuringBuild" in result.stdout, result.stdout

        original = build()
        assert original[1] == 1
        assert build() == original, "unchanged inputs must reuse configuration and the executable"
        (root / "src/cli/browser/app.ts").write_text("changed UI")
        (root / "README.md").write_text("changed documentation")
        assert build() == original, "UI and documentation must not rebuild the compiler"

        (root / "src/nested/input.zig").write_text(input_text("other"))
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

        (root / "include/revision.h").write_text("#define REVISION 2\n")
        header = build()
        assert header[0] != changed[0] and header[1] == 1 and header[4] == 2
        assert build() == header

        # Edit after translation. An unordered generator would capture revision
        # 3's identity while retaining revision 2's compiled bindings and pass.
        (root / "header-mutation").write_text("#define REVISION 3\n")
        build(success=False)
        assert installed() == header, "rejected translation must not replace the installed artifact"
        translated = build()
        assert translated[0] != header[0] and translated[1] == 1 and translated[4] == 3
        assert build() == translated, "retry must reuse consistent translated bindings"

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
            (root / "src/nested/input.zig").write_text(input_text("concurrent configuration edit"))
            (root / "configure-resume").write_text("resume")
            output = process.communicate(timeout=90)[0]
            assert process.returncode == 0, output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        raced = installed()
        assert raced[0] != translated[0] and raced[1] == 2 and raced[3] == "concurrent configuration edit" and raced[4] == 3
        assert build(["-Drace=true"]) == raced

        # A later edit must fail validation, then recover on an unchanged retry.
        (root / "mutation").write_text(input_text("edit after compilation"))
        build(["-Drace=true"], success=False)
        assert installed() == raced, "a rejected build must not replace the installed artifact"
        recovered = build(["-Drace=true"])
        assert recovered[0] != raced[0] and recovered[1] == 2, "retry must recover without reconfiguration"
        assert build(["-Drace=true"]) == recovered
    print("Compiler identity: compiled source/header values, configure reuse, concurrent translation/configuration edits, validation failure and retry recovery passed")


if __name__ == "__main__":
    main()
