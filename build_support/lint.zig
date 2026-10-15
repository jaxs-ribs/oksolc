// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

const std = @import("std");

/// Reuse the parent's resolved package directory, including --system and
/// --pkg-dir overrides. yyjson has no package manifest and cannot be --fork'ed,
/// so its root identifies the package store rather than a source override.
pub fn worker(b: *std.Build, yyjson: *std.Build.Dependency) *std.Build.Step.Run {
    const package_path = yyjson.builder.root.toString(b.allocator) catch @panic("OOM");
    const package_directory = std.Io.Dir.path.dirname(package_path) orelse ".";
    const run = std.Build.Step.Run.create(b, "configure and run zlinter");
    run.addFileArg2(.zig_exe, .{ .make_absolute = true });
    run.addArgs(&.{ "build", "lint", "-Dlint-worker=true", "--pkg-dir" });
    const package_dir = b.graph.cwdRelativePath(package_directory);
    run.addDirectoryArg2(package_dir, .{ .make_absolute = true });
    if (b.graph.system_package_mode) {
        run.addArg("--system");
        run.addDirectoryArg2(package_dir, .{ .make_absolute = true });
    }
    // Preserve source overrides rather than resolving their original URLs.
    for (std.Build.package_map.values()) |entry| {
        if (!entry.available or entry.run_build == null or std.mem.eql(u8, entry.hash, b.pkg_hash)) continue;
        // Local dependencies use path keys, not the snapshot hashes targeted by --fork.
        if (entry.hash.len < 44 or std.mem.findScalar(u8, entry.hash, '/') != null or
            std.mem.findScalar(u8, entry.hash, '\\') != null) continue;
        const expected = std.Io.Dir.path.join(b.allocator, &.{ package_directory, entry.hash }) catch @panic("OOM");
        if (std.mem.eql(u8, entry.build_root, expected)) continue;
        run.addArg("--fork");
        run.addDirectoryArg2(b.graph.cwdRelativePath(entry.build_root), .{ .make_absolute = true });
    }
    for (b.user_input_options.keys(), b.user_input_options.values()) |name, value| {
        if (std.mem.eql(u8, name, "lint-worker")) continue;
        switch (value) {
            .flag => run.addArg(b.fmt("-D{s}", .{name})),
            .scalar => |scalar| run.addArg(b.fmt("-D{s}={s}", .{ name, scalar })),
            .list => |list| for (list.items) |scalar| {
                run.addArg(b.fmt("-D{s}={s}", .{ name, scalar }));
            },
            else => @panic("lint requires command-line build options"),
        }
    }
    if (b.graph.release_mode != .off)
        run.addArg(b.fmt("--release={s}", .{@tagName(b.graph.release_mode)}));
    for (b.graph.system_integration_options.keys(), b.graph.system_integration_options.values()) |name, mode| {
        switch (mode) {
            .user_enabled, .declared_enabled => run.addArg(b.fmt("-fsys={s}", .{name})),
            .user_disabled, .declared_disabled => run.addArg(b.fmt("-fno-sys={s}", .{name})),
        }
    }
    run.addArg("--cache-dir");
    run.addDirectoryArg2(b.graph.path(.local_cache, ""), .{ .make_absolute = true });
    run.addArg("--");
    run.addPassthruArgs();
    run.setCwd(b.path("."));
    run.has_side_effects = true;
    return run;
}
