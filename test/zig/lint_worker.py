#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

"""Check the real lint launcher with isolated packages and a recording worker."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile


BUILD = r'''
const std = @import("std");
pub fn build(b: *std.Build) void {
    const worker = b.option(bool, "lint-worker", "Internal lint worker") orelse false;
    const flavor = b.option([]const u8, "flavor", "Fixture option") orelse "default";
    const target = b.standardTargetOptions(.{});
    _ = b.standardOptimizeOption(.{});
    const integration = b.systemIntegrationOption("fixture", .{ .default = false });
    const yyjson = b.dependency("yyjson", .{});
    const tool = b.dependency("fixture_tool", .{});
    const lint = b.step("lint", "Record lint invocation");
    if (worker) {
        const run = b.addSystemCommand(&.{"python3"});
        run.addFileArg2(b.path("record.py"), .{});
        run.addArgs(&.{
            if (b.graph.system_package_mode) "system" else "normal",
            flavor,
            target.query.zigTriple(b.allocator) catch @panic("OOM"),
            @tagName(b.graph.release_mode),
            if (integration) "enabled" else "disabled",
        });
        run.addFileArg2(tool.path("marker"), .{});
        run.addPassthruArgs();
        run.setCwd(b.path("."));
        run.has_side_effects = true;
        lint.dependOn(&run.step);
    } else lint.dependOn(&@import("build_support/lint.zig").worker(b, yyjson).step);
}
'''

RECORD = '''import json
from pathlib import Path
import sys
mode, flavor, target, release, integration, marker, *arguments = sys.argv[1:]
Path("result.json").write_text(json.dumps({
    "mode": mode, "flavor": flavor, "target": target,
    "release": release, "integration": integration,
    "tool": Path(marker).read_text(), "arguments": arguments,
}))
'''


def manifest(paths, dependencies=""):
    return (
        '.{ .name = .solidity_zig, .version = "0.0.0", .fingerprint = 0x532998ed66b1991d, '
        + dependencies + '.paths = .{'
        + ','.join(json.dumps(path) for path in paths) + '} }\n'
    )


def main():
    source = Path(__file__).resolve().parents[2]
    zig = sys.argv[1] if len(sys.argv) > 1 else "zig"
    # This unstructured C dependency cannot be --fork'ed, just like real yyjson.
    package_hash = "N-V-__8AADJobAAfcT3EULUqTBZto6rfWBxzeDyfBxrSCP_b"
    tool_hash = "solidity_zig-0.0.0-" + base64.urlsafe_b64encode(
        struct.pack("<II", 0x66b1991d, 0) + bytes(25)
    ).decode().rstrip("=")
    with tempfile.TemporaryDirectory(prefix="oksolc-lint-worker-") as temporary:
        root = Path(temporary)
        (root / "build_support").mkdir()
        shutil.copyfile(source / "build_support/lint.zig", root / "build_support/lint.zig")
        (root / "build.zig").write_text(BUILD)
        (root / "record.py").write_text(RECORD)
        packages = root / "provided-packages"
        (packages / package_hash).mkdir(parents=True)
        dependencies = (
            '.dependencies = .{ .yyjson = .{ .url = "https://example.invalid/yyjson.tar.gz", '
            '.hash = "' + package_hash + '" }, .fixture_tool = .{ '
            '.url = "https://example.invalid/tool.tar.gz", .hash = "' + tool_hash + '" } }, '
        )
        (root / "build.zig.zon").write_text(manifest(
            ("build.zig", "build.zig.zon", "build_support", "record.py"), dependencies
        ))
        for tool, marker in ((packages / tool_hash, "original"), (root / "overridden-tool", "overridden")):
            tool.mkdir()
            (tool / "build.zig").write_text('pub fn build(_: *@import("std").Build) void {}\n')
            (tool / "build.zig.zon").write_text(manifest(("build.zig", "build.zig.zon", "marker")))
            (tool / "marker").write_text(marker)
        environment = dict(os.environ)
        environment.update({
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
            # The explicit CLI package directory must beat an inherited override.
            "ZIG_LOCAL_PKG_DIR": str(root / "incorrect-packages"),
        })
        arguments = ["--fix", "--filter", "file with spaces.zig", "--format", "json", "--help"]
        base = [
            zig, "build", "lint", "--pkg-dir", str(packages),
            "-Dflavor=chosen value", "-Dtarget=aarch64-linux-gnu", "--release=fast",
            "-fsys=fixture", "--cache-poison=disallowed",
            f"-j{min(os.cpu_count() or 1, 6)}", "--summary", "failures",
        ]

        def check(extra, mode, tool):
            result = subprocess.run(
                base + extra + ["--"] + arguments, cwd=root, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90,
            )
            assert result.returncode == 0, result.stdout
            recorded = json.loads((root / "result.json").read_text())
            assert recorded == {
                "mode": mode, "flavor": "chosen value", "target": "aarch64-linux-gnu",
                "release": "fast", "integration": "enabled", "tool": tool,
                "arguments": arguments,
            }, recorded
            assert not (root / "zig-pkg").exists(), "worker must reuse the parent's package sources"

        check(["--system", str(packages)], "system", "original")
        check([], "normal", "original")
        check(["--system", str(packages), "--fork", str(root / "overridden-tool")], "system", "overridden")
    print("Lint worker: passthrough, offline system packages, package overrides, source forks and build options passed")


if __name__ == "__main__":
    main()
